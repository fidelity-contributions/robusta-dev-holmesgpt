import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, OrderedDict, Tuple, Union

from jinja2 import Template
from pydantic import BaseModel, ConfigDict, Field, FilePath, model_validator
from rich.console import Console

from holmes.core.openai_formatting import format_tool_to_open_ai_standard
from holmes.plugins.prompts import load_and_render_prompt
import time
from rich.table import Table


class ToolResultStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    NO_DATA = "no_data"

    def to_color(self) -> str:
        if self == ToolResultStatus.SUCCESS:
            return "green"
        elif self == ToolResultStatus.ERROR:
            return "red"
        else:
            return "white"

    def to_emoji(self) -> str:
        if self == ToolResultStatus.SUCCESS:
            return "✔"
        elif self == ToolResultStatus.ERROR:
            return "❌"
        else:
            return "⚪️"


class StructuredToolResult(BaseModel):
    schema_version: str = "robusta:v1.0.0"
    status: ToolResultStatus
    error: Optional[str] = None
    return_code: Optional[int] = None
    data: Optional[Any] = None
    url: Optional[str] = None
    invocation: Optional[str] = None
    params: Optional[Dict] = None

    def get_stringified_data(self) -> str:
        if self.data is None:
            return ""

        if isinstance(self.data, str):
            return self.data
        else:
            try:
                if isinstance(self.data, BaseModel):
                    return self.data.model_dump_json(indent=2)
                else:
                    return json.dumps(self.data, indent=2)
            except Exception:
                return str(self.data)


def sanitize(param):
    # allow empty strings to be unquoted - useful for optional params
    # it is up to the user to ensure that the command they are using is ok with empty strings
    # and if not to take that into account via an appropriate jinja template
    if param == "":
        return ""

    return shlex.quote(str(param))


def sanitize_params(params):
    return {k: sanitize(str(v)) for k, v in params.items()}


def format_tool_output(tool_result: Union[str, StructuredToolResult]) -> str:
    if isinstance(tool_result, StructuredToolResult):
        if tool_result.data and isinstance(tool_result.data, str):
            # Display logs and other string outputs in a way that is readable to humans.
            # To do this, we extract them from the result and print them as-is below.
            # The metadata is printed on a single line to
            data = tool_result.data
            tool_result.data = "The raw tool data is printed below this JSON"
            result_str = tool_result.model_dump_json(indent=2, exclude_none=True)
            result_str += f"\n{data}"
            return result_str
        else:
            return tool_result.model_dump_json(indent=2)
    else:
        return tool_result


class ToolsetStatusEnum(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    FAILED = "failed"


class ToolsetTag(str, Enum):
    CORE = "core"
    CLUSTER = "cluster"
    CLI = "cli"


class ToolsetType(str, Enum):
    BUILTIN = "built-in"
    CUSTOMIZED = "custom"
    MCP = "mcp"


class ToolParameter(BaseModel):
    description: Optional[str] = None
    type: str = "string"
    required: bool = True


class Tool(ABC, BaseModel):
    name: str
    description: str
    parameters: Dict[str, ToolParameter] = {}
    user_description: Optional[str] = (
        None  # templated string to show to the user describing this tool invocation (not seen by llm)
    )
    additional_instructions: Optional[str] = None

    def get_openai_format(self):
        return format_tool_to_open_ai_standard(
            tool_name=self.name,
            tool_description=self.description,
            tool_parameters=self.parameters,
        )

    def invoke(
        self, params: Dict, tool_number: Optional[int] = None
    ) -> StructuredToolResult:
        tool_number_str = f"#{tool_number} " if tool_number else ""
        logging.info(
            f"Running tool {tool_number_str}[bold]{self.name}[/bold]: {self.get_parameterized_one_liner(params)}"
        )
        start_time = time.time()
        result = self._invoke(params)
        elapsed = time.time() - start_time
        output_str = (
            result.get_stringified_data()
            if hasattr(result, "get_stringified_data")
            else str(result)
        )
        show_hint = f"/show {tool_number}" if tool_number else "/show"
        line_count = output_str.count("\n") + 1 if output_str else 0
        logging.info(
            f"  [dim]Finished {tool_number_str}in {elapsed:.2f}s, output length: {len(output_str):,} characters ({line_count:,} lines) - {show_hint} to view contents[/dim]"
        )
        return result

    @abstractmethod
    def _invoke(self, params: Dict) -> StructuredToolResult:
        pass

    @abstractmethod
    def get_parameterized_one_liner(self, params: Dict) -> str:
        return ""


class YAMLTool(Tool, BaseModel):
    command: Optional[str] = None
    script: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)
        self.__infer_parameters()

    def __infer_parameters(self):
        # Find parameters that appear inside self.command or self.script but weren't declared in parameters
        template = self.command or self.script
        inferred_params = re.findall(r"\{\{\s*([\w]+)[\.\|]?.*?\s*\}\}", template)
        # TODO: if filters were used in template, take only the variable name
        # Regular expression to match Jinja2 placeholders with or without filters
        # inferred_params = re.findall(r'\{\{\s*(\w+)(\s*\|\s*[^}]+)?\s*\}\}', self.command)
        # for param_tuple in inferred_params:
        #    param = param_tuple[0]  # Extract the parameter name
        #    if param not in self.parameters:
        #        self.parameters[param] = ToolParameter()
        for param in inferred_params:
            if param not in self.parameters:
                self.parameters[param] = ToolParameter()

    def get_parameterized_one_liner(self, params) -> str:
        params = sanitize_params(params)
        if self.user_description:
            template = Template(self.user_description)
        else:
            cmd_or_script = self.command or self.script
            template = Template(cmd_or_script)  # type: ignore
        return template.render(params)

    def _build_context(self, params):
        params = sanitize_params(params)
        context = {**params}
        return context

    def _get_status(self, return_code: int, raw_output: str) -> ToolResultStatus:
        if return_code != 0:
            return ToolResultStatus.ERROR
        if raw_output == "":
            return ToolResultStatus.NO_DATA
        return ToolResultStatus.SUCCESS

    def _invoke(self, params) -> StructuredToolResult:
        if self.command is not None:
            raw_output, return_code, invocation = self.__invoke_command(params)
        else:
            raw_output, return_code, invocation = self.__invoke_script(params)  # type: ignore

        if self.additional_instructions and return_code == 0:
            logging.info(
                f"Applying additional instructions: {self.additional_instructions}"
            )
            output_with_instructions = self.__apply_additional_instructions(raw_output)
        else:
            output_with_instructions = raw_output

        error = (
            None
            if return_code == 0
            else f"Command `{invocation}` failed with return code {return_code}\nOutput:\n{raw_output}"
        )
        status = self._get_status(return_code, raw_output)

        return StructuredToolResult(
            status=status,
            error=error,
            return_code=return_code,
            data=output_with_instructions,
            params=params,
            invocation=invocation,
        )

    def __apply_additional_instructions(self, raw_output: str) -> str:
        try:
            result = subprocess.run(
                self.additional_instructions,  # type: ignore
                input=raw_output,
                shell=True,
                text=True,
                capture_output=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            logging.error(
                f"Failed to apply additional instructions: {self.additional_instructions}. "
                f"Error: {e.stderr}"
            )
            return f"Error applying additional instructions: {e.stderr}"

    def __invoke_command(self, params) -> Tuple[str, int, str]:
        context = self._build_context(params)
        command = os.path.expandvars(self.command)  # type: ignore
        template = Template(command)  # type: ignore
        rendered_command = template.render(context)
        output, return_code = self.__execute_subprocess(rendered_command)
        return output, return_code, rendered_command

    def __invoke_script(self, params) -> str:
        context = self._build_context(params)
        script = os.path.expandvars(self.script)  # type: ignore
        template = Template(script)  # type: ignore
        rendered_script = template.render(context)

        with tempfile.NamedTemporaryFile(
            mode="w+", delete=False, suffix=".sh"
        ) as temp_script:
            temp_script.write(rendered_script)
            temp_script_path = temp_script.name
        subprocess.run(["chmod", "+x", temp_script_path], check=True)

        try:
            output, return_code = self.__execute_subprocess(temp_script_path)
        finally:
            subprocess.run(["rm", temp_script_path])
        return output, return_code, rendered_script  # type: ignore

    def __execute_subprocess(self, cmd) -> Tuple[str, int]:
        try:
            logging.debug(f"Running `{cmd}`")
            result = subprocess.run(
                cmd,
                shell=True,
                text=True,
                check=False,  # do not throw error, we just return the error code
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            return result.stdout.strip(), result.returncode
        except Exception as e:
            logging.error(
                f"An unexpected error occurred while running '{cmd}': {e}",
                exc_info=True,
            )
            output = f"Command execution failed with error: {e}"
            return output, 1


class StaticPrerequisite(BaseModel):
    enabled: bool
    disabled_reason: str


class CallablePrerequisite(BaseModel):
    callable: Callable[[dict[str, Any]], Tuple[bool, str]]


class ToolsetCommandPrerequisite(BaseModel):
    command: str  # must complete successfully (error code 0) for prereq to be satisfied
    expected_output: Optional[str] = None  # optional


class ToolsetEnvironmentPrerequisite(BaseModel):
    env: List[str] = []  # optional


class Toolset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experimental: bool = False

    enabled: bool = False
    name: str
    description: str
    docs_url: Optional[str] = None
    icon_url: Optional[str] = None
    installation_instructions: Optional[str] = None
    additional_instructions: Optional[str] = ""
    prerequisites: List[
        Union[
            StaticPrerequisite,
            ToolsetCommandPrerequisite,
            ToolsetEnvironmentPrerequisite,
            CallablePrerequisite,
        ]
    ] = []
    tools: List[Tool]
    tags: List[ToolsetTag] = Field(
        default_factory=lambda: [ToolsetTag.CORE],
    )
    config: Optional[Any] = None
    is_default: bool = False
    llm_instructions: Optional[str] = None

    # warning! private attributes are not copied, which can lead to subtle bugs.
    # e.g. l.extend([some_tool]) will reset these private attribute to None

    # status fields that be cached
    type: Optional[ToolsetType] = None
    path: Optional[FilePath] = None
    status: ToolsetStatusEnum = ToolsetStatusEnum.DISABLED
    error: Optional[str] = None

    def override_with(self, override: "Toolset") -> None:
        """
        Overrides the current attributes with values from the Toolset loaded from custom config
        if they are not None.
        """
        for field, value in override.model_dump(
            exclude_unset=True,
            exclude=("name"),  # type: ignore
        ).items():
            if field in self.__class__.model_fields and value not in (None, [], {}, ""):
                setattr(self, field, value)

    @model_validator(mode="before")
    def preprocess_tools(cls, values):
        additional_instructions = values.get("additional_instructions", "")
        tools_data = values.get("tools", [])
        tools = []
        for tool in tools_data:
            if isinstance(tool, dict):
                tool["additional_instructions"] = additional_instructions
            if isinstance(tool, Tool):
                tool.additional_instructions = additional_instructions
            tools.append(tool)
        values["tools"] = tools

        return values

    def get_environment_variables(self) -> List[str]:
        env_vars = set()

        for prereq in self.prerequisites:
            if isinstance(prereq, ToolsetEnvironmentPrerequisite):
                env_vars.update(prereq.env)
        return list(env_vars)

    def interpolate_command(self, command: str) -> str:
        interpolated_command = os.path.expandvars(command)

        return interpolated_command

    def check_prerequisites(self):
        self.status = ToolsetStatusEnum.ENABLED

        for prereq in self.prerequisites:
            if isinstance(prereq, ToolsetCommandPrerequisite):
                try:
                    command = self.interpolate_command(prereq.command)
                    result = subprocess.run(
                        command,
                        shell=True,
                        check=True,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    if (
                        prereq.expected_output
                        and prereq.expected_output not in result.stdout
                    ):
                        self.status = ToolsetStatusEnum.FAILED
                        self.error = f"`{prereq.command}` did not include `{prereq.expected_output}`"
                except subprocess.CalledProcessError as e:
                    self.status = ToolsetStatusEnum.FAILED
                    self.error = f"`{prereq.command}` returned {e.returncode}"

            elif isinstance(prereq, ToolsetEnvironmentPrerequisite):
                for env_var in prereq.env:
                    if env_var not in os.environ:
                        self.status = ToolsetStatusEnum.FAILED
                        self.error = f"Environment variable {env_var} was not set"

            elif isinstance(prereq, StaticPrerequisite):
                if not prereq.enabled:
                    self.status = ToolsetStatusEnum.FAILED
                    self.error = f"{prereq.disabled_reason}"

            elif isinstance(prereq, CallablePrerequisite):
                try:
                    (enabled, error_message) = prereq.callable(self.config)
                    if not enabled:
                        self.status = ToolsetStatusEnum.FAILED
                    if error_message:
                        self.error = f"{error_message}"
                except Exception as e:
                    self.status = ToolsetStatusEnum.FAILED
                    self.error = f"Prerequisite call failed unexpectedly: {str(e)}"

            if (
                self.status == ToolsetStatusEnum.DISABLED
                or self.status == ToolsetStatusEnum.FAILED
            ):
                logging.info(f"❌ Toolset {self.name}: {self.error}")
                # no point checking further prerequisites if one failed
                return

        logging.info(f"✅ Toolset {self.name}")

    @abstractmethod
    def get_example_config(self) -> Dict[str, Any]:
        return {}

    def _load_llm_instructions(self, jinja_template: str):
        tool_names = [t.name for t in self.tools]
        self.llm_instructions = load_and_render_prompt(
            prompt=jinja_template,
            context={"tool_names": tool_names, "config": self.config},
        )


class YAMLToolset(Toolset):
    tools: List[YAMLTool]  # type: ignore

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.llm_instructions:
            self._load_llm_instructions(self.llm_instructions)

    def get_example_config(self) -> Dict[str, Any]:
        return {}


class ToolsetYamlFromConfig(Toolset):
    """
    ToolsetYamlFromConfig represents a toolset loaded from a YAML configuration file.
    To override a build-in toolset fields, we don't have to explicitly set all required fields,
    instead, we only put the fields we want to override in the YAML file.
    ToolsetYamlFromConfig helps py-pass the pydantic validation of the required fields and together with
    `override_with` method, a build-in toolset object with new configurations is created.
    """

    name: str
    # YamlToolset is loaded from a YAML file specified by the user and should be enabled by default
    # Built-in toolsets are exception and should be disabled by default when loaded
    enabled: bool = True
    additional_instructions: Optional[str] = None
    prerequisites: List[
        Union[
            StaticPrerequisite,
            ToolsetCommandPrerequisite,
            ToolsetEnvironmentPrerequisite,
        ]
    ] = []  # type: ignore
    tools: Optional[List[YAMLTool]] = []  # type: ignore
    description: Optional[str] = None  # type: ignore
    docs_url: Optional[str] = None
    icon_url: Optional[str] = None
    installation_instructions: Optional[str] = None
    config: Optional[Any] = None
    url: Optional[str] = None  # MCP toolset

    def get_example_config(self) -> Dict[str, Any]:
        return {}


class ToolsetDBModel(BaseModel):
    account_id: str
    cluster_id: str
    toolset_name: str
    icon_url: Optional[str] = None
    status: Optional[str] = None
    error: Optional[str] = None
    description: Optional[str] = None
    docs_url: Optional[str] = None
    installation_instructions: Optional[str] = None
    updated_at: str = Field(default_factory=datetime.now().isoformat)


def pretty_print_toolset_status(toolsets: list[Toolset], console: Console) -> None:
    status_fields = ["name", "enabled", "status", "type", "path", "error"]
    toolsets_status = []
    for toolset in sorted(toolsets, key=lambda ts: ts.status.value):
        toolset_status = json.loads(toolset.model_dump_json(include=status_fields))  # type: ignore

        status_value = toolset_status.get("status", "")
        error_value = toolset_status.get("error", "")
        if status_value == "enabled":
            toolset_status["status"] = "[green]enabled[/green]"
        elif status_value == "failed":
            toolset_status["status"] = "[red]failed[/red]"
            toolset_status["error"] = f"[red]{error_value}[/red]"
        else:
            toolset_status["status"] = f"[yellow]{status_value}[/yellow]"

        # Replace None with "" for Path and Error columns
        for field in ["path", "error"]:
            if toolset_status.get(field) is None:
                toolset_status[field] = ""

        order_toolset_status = OrderedDict(
            (k.capitalize(), toolset_status[k])
            for k in status_fields
            if k in toolset_status
        )
        toolsets_status.append(order_toolset_status)

    table = Table(show_header=True, header_style="bold")
    for col in status_fields:
        table.add_column(col.capitalize())

    for row in toolsets_status:
        table.add_row(*(str(row.get(col.capitalize(), "")) for col in status_fields))

    console.print(table)
