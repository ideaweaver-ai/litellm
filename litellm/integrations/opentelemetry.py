import os
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union, cast

import litellm
from litellm._logging import verbose_logger
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.safe_json_dumps import safe_dumps
from litellm.types.services import ServiceLoggerPayload
from litellm.types.utils import (
    ChatCompletionMessageToolCall,
    Function,
    StandardCallbackDynamicParams,
    StandardLoggingPayload,
)

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export import SpanExporter as _SpanExporter
    from opentelemetry.trace import Context as _Context
    from opentelemetry.trace import Span as _Span

    from litellm.proxy._types import (
        ManagementEndpointLoggingPayload as _ManagementEndpointLoggingPayload,
    )
    from litellm.proxy.proxy_server import UserAPIKeyAuth as _UserAPIKeyAuth

    Span = Union[_Span, Any]
    Context = Union[_Context, Any]
    SpanExporter = Union[_SpanExporter, Any]
    UserAPIKeyAuth = Union[_UserAPIKeyAuth, Any]
    ManagementEndpointLoggingPayload = Union[_ManagementEndpointLoggingPayload, Any]
else:
    Span = Any
    SpanExporter = Any
    UserAPIKeyAuth = Any
    ManagementEndpointLoggingPayload = Any
    Context = Any

LITELLM_TRACER_NAME = os.getenv("OTEL_TRACER_NAME", "litellm")
LITELLM_RESOURCE: Dict[Any, Any] = {
    "service.name": os.getenv("OTEL_SERVICE_NAME", "litellm"),
    "deployment.environment": os.getenv("OTEL_ENVIRONMENT_NAME", "production"),
    "model_id": os.getenv("OTEL_SERVICE_NAME", "litellm"),
}
RAW_REQUEST_SPAN_NAME = "raw_gen_ai_request"
LITELLM_REQUEST_SPAN_NAME = "litellm_request"


@dataclass
class OpenTelemetryConfig:
    exporter: Union[str, SpanExporter] = "console"
    endpoint: Optional[str] = None
    headers: Optional[str] = None

    @classmethod
    def from_env(cls):
        """
        OTEL_HEADERS=x-honeycomb-team=B85YgLm9****
        OTEL_EXPORTER="otlp_http"
        OTEL_ENDPOINT="https://api.honeycomb.io/v1/traces"

        OTEL_HEADERS gets sent as headers = {"x-honeycomb-team": "B85YgLm96******"}
        """
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exporter = os.getenv(
            "OTEL_EXPORTER_OTLP_PROTOCOL", os.getenv("OTEL_EXPORTER", "console")
        )
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", os.getenv("OTEL_ENDPOINT"))
        headers = os.getenv(
            "OTEL_EXPORTER_OTLP_HEADERS", os.getenv("OTEL_HEADERS")
        )  # example: OTEL_HEADERS=x-honeycomb-team=B85YgLm96***"

        if exporter == "in_memory":
            return cls(exporter=InMemorySpanExporter())
        return cls(
            exporter=exporter,
            endpoint=endpoint,
            headers=headers,  # example: OTEL_HEADERS=x-honeycomb-team=B85YgLm96***"
        )


class OpenTelemetry(CustomLogger):
    def __init__(
        self,
        config: Optional[OpenTelemetryConfig] = None,
        callback_name: Optional[str] = None,
        **kwargs,
    ):
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.trace import SpanKind

        if config is None:
            config = OpenTelemetryConfig.from_env()

        self.config = config
        self.OTEL_EXPORTER = self.config.exporter
        self.OTEL_ENDPOINT = self.config.endpoint
        self.OTEL_HEADERS = self.config.headers
        provider = TracerProvider(resource=Resource(attributes=LITELLM_RESOURCE))
        provider.add_span_processor(self._get_span_processor())
        self.callback_name = callback_name

        trace.set_tracer_provider(provider)
        self.tracer = trace.get_tracer(LITELLM_TRACER_NAME)

        self.span_kind = SpanKind

        _debug_otel = str(os.getenv("DEBUG_OTEL", "False")).lower()

        if _debug_otel == "true":
            # Set up logging
            import logging

            logging.basicConfig(level=logging.DEBUG)
            logging.getLogger(__name__)

            # Enable OpenTelemetry logging
            otel_exporter_logger = logging.getLogger("opentelemetry.sdk.trace.export")
            otel_exporter_logger.setLevel(logging.DEBUG)

        # init CustomLogger params
        super().__init__(**kwargs)
        self._init_otel_logger_on_litellm_proxy()

    def _init_otel_logger_on_litellm_proxy(self):
        """
        Initializes OpenTelemetry for litellm proxy server

        - Adds Otel as a service callback
        - Sets `proxy_server.open_telemetry_logger` to self
        """
        try:
            from litellm.proxy import proxy_server
        except ImportError:
            verbose_logger.warning(
                "Proxy Server is not installed. Skipping OpenTelemetry initialization."
            )
            return

        # Add Otel as a service callback
        if "otel" not in litellm.service_callback:
            litellm.service_callback.append("otel")
        setattr(proxy_server, "open_telemetry_logger", self)

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._handle_sucess(kwargs, response_obj, start_time, end_time)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._handle_failure(kwargs, response_obj, start_time, end_time)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._handle_sucess(kwargs, response_obj, start_time, end_time)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._handle_failure(kwargs, response_obj, start_time, end_time)

    async def async_service_success_hook(
        self,
        payload: ServiceLoggerPayload,
        parent_otel_span: Optional[Span] = None,
        start_time: Optional[Union[datetime, float]] = None,
        end_time: Optional[Union[datetime, float]] = None,
        event_metadata: Optional[dict] = None,
    ):
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        _start_time_ns = 0
        _end_time_ns = 0

        if isinstance(start_time, float):
            _start_time_ns = int(start_time * 1e9)
        else:
            _start_time_ns = self._to_ns(start_time)

        if isinstance(end_time, float):
            _end_time_ns = int(end_time * 1e9)
        else:
            _end_time_ns = self._to_ns(end_time)

        if parent_otel_span is not None:
            _span_name = payload.service
            service_logging_span = self.tracer.start_span(
                name=_span_name,
                context=trace.set_span_in_context(parent_otel_span),
                start_time=_start_time_ns,
            )
            self.safe_set_attribute(
                span=service_logging_span,
                key="call_type",
                value=payload.call_type,
            )
            self.safe_set_attribute(
                span=service_logging_span,
                key="service",
                value=payload.service.value,
            )

            if event_metadata:
                for key, value in event_metadata.items():
                    if value is None:
                        value = "None"
                    if isinstance(value, dict):
                        try:
                            value = str(value)
                        except Exception:
                            value = "litellm logging error - could_not_json_serialize"
                    self.safe_set_attribute(
                        span=service_logging_span,
                        key=key,
                        value=value,
                    )
            service_logging_span.set_status(Status(StatusCode.OK))
            service_logging_span.end(end_time=_end_time_ns)

    async def async_service_failure_hook(
        self,
        payload: ServiceLoggerPayload,
        error: Optional[str] = "",
        parent_otel_span: Optional[Span] = None,
        start_time: Optional[Union[datetime, float]] = None,
        end_time: Optional[Union[float, datetime]] = None,
        event_metadata: Optional[dict] = None,
    ):
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        _start_time_ns = 0
        _end_time_ns = 0

        if isinstance(start_time, float):
            _start_time_ns = int(int(start_time) * 1e9)
        else:
            _start_time_ns = self._to_ns(start_time)

        if isinstance(end_time, float):
            _end_time_ns = int(int(end_time) * 1e9)
        else:
            _end_time_ns = self._to_ns(end_time)

        if parent_otel_span is not None:
            _span_name = payload.service
            service_logging_span = self.tracer.start_span(
                name=_span_name,
                context=trace.set_span_in_context(parent_otel_span),
                start_time=_start_time_ns,
            )
            self.safe_set_attribute(
                span=service_logging_span,
                key="call_type",
                value=payload.call_type,
            )
            self.safe_set_attribute(
                span=service_logging_span,
                key="service",
                value=payload.service.value,
            )
            if error:
                self.safe_set_attribute(
                    span=service_logging_span,
                    key="error",
                    value=error,
                )
            if event_metadata:
                for key, value in event_metadata.items():
                    if isinstance(value, dict):
                        try:
                            value = str(value)
                        except Exception:
                            value = "litllm logging error - could_not_json_serialize"
                    self.safe_set_attribute(
                        span=service_logging_span,
                        key=key,
                        value=value,
                    )

            service_logging_span.set_status(Status(StatusCode.ERROR))
            service_logging_span.end(end_time=_end_time_ns)

    async def async_post_call_failure_hook(
        self,
        request_data: dict,
        original_exception: Exception,
        user_api_key_dict: UserAPIKeyAuth,
        traceback_str: Optional[str] = None,
    ):
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        parent_otel_span = user_api_key_dict.parent_otel_span
        if parent_otel_span is not None:
            parent_otel_span.set_status(Status(StatusCode.ERROR))
            _span_name = "Failed Proxy Server Request"

            # Exception Logging Child Span
            exception_logging_span = self.tracer.start_span(
                name=_span_name,
                context=trace.set_span_in_context(parent_otel_span),
            )
            self.safe_set_attribute(
                span=exception_logging_span,
                key="exception",
                value=str(original_exception),
            )
            exception_logging_span.set_status(Status(StatusCode.ERROR))
            exception_logging_span.end(end_time=self._to_ns(datetime.now()))

            # End Parent OTEL Sspan
            parent_otel_span.end(end_time=self._to_ns(datetime.now()))

    def _handle_sucess(self, kwargs, response_obj, start_time, end_time):
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        verbose_logger.debug(
            "OpenTelemetry Logger: Logging kwargs: %s, OTEL config settings=%s",
            kwargs,
            self.config,
        )
        _parent_context, parent_otel_span = self._get_span_context(kwargs)

        self._add_dynamic_span_processor_if_needed(kwargs)

        # Span 1: Requst sent to litellm SDK
        span = self.tracer.start_span(
            name=self._get_span_name(kwargs),
            start_time=self._to_ns(start_time),
            context=_parent_context,
        )
        span.set_status(Status(StatusCode.OK))
        self.set_attributes(span, kwargs, response_obj)

        if litellm.turn_off_message_logging is True:
            pass
        elif self.message_logging is not True:
            pass
        else:
            # Span 2: Raw Request / Response to LLM
            raw_request_span = self.tracer.start_span(
                name=RAW_REQUEST_SPAN_NAME,
                start_time=self._to_ns(start_time),
                context=trace.set_span_in_context(span),
            )

            raw_request_span.set_status(Status(StatusCode.OK))
            self.set_raw_request_attributes(raw_request_span, kwargs, response_obj)
            raw_request_span.end(end_time=self._to_ns(end_time))

        span.end(end_time=self._to_ns(end_time))

        # Create span for guardrail information
        self._create_guardrail_span(kwargs=kwargs, context=_parent_context)

        if parent_otel_span is not None:
            parent_otel_span.end(end_time=self._to_ns(datetime.now()))

    def _create_guardrail_span(
        self, kwargs: Optional[dict], context: Optional[Context]
    ):
        """
        Creates a span for Guardrail, if any guardrail information is present in standard_logging_object
        """
        # Create span for guardrail information
        kwargs = kwargs or {}
        standard_logging_payload: Optional[StandardLoggingPayload] = kwargs.get(
            "standard_logging_object"
        )
        if standard_logging_payload is None:
            return

        guardrail_information = standard_logging_payload.get("guardrail_information")
        if guardrail_information is None:
            return

        start_time_float = guardrail_information.get("start_time")
        end_time_float = guardrail_information.get("end_time")
        start_time_datetime = datetime.now()
        if start_time_float is not None:
            start_time_datetime = datetime.fromtimestamp(start_time_float)
        end_time_datetime = datetime.now()
        if end_time_float is not None:
            end_time_datetime = datetime.fromtimestamp(end_time_float)

        guardrail_span = self.tracer.start_span(
            name="guardrail",
            start_time=self._to_ns(start_time_datetime),
            context=context,
        )

        self.safe_set_attribute(
            span=guardrail_span,
            key="guardrail_name",
            value=guardrail_information.get("guardrail_name"),
        )

        self.safe_set_attribute(
            span=guardrail_span,
            key="guardrail_mode",
            value=guardrail_information.get("guardrail_mode"),
        )

        # Set masked_entity_count directly without conversion
        masked_entity_count = guardrail_information.get("masked_entity_count")
        if masked_entity_count is not None:
            guardrail_span.set_attribute(
                "masked_entity_count", safe_dumps(masked_entity_count)
            )

        self.safe_set_attribute(
            span=guardrail_span,
            key="guardrail_response",
            value=guardrail_information.get("guardrail_response"),
        )

        guardrail_span.end(end_time=self._to_ns(end_time_datetime))

    def _add_dynamic_span_processor_if_needed(self, kwargs):
        """
        Helper method to add a span processor with dynamic headers if needed.

        This allows for per-request configuration of telemetry exporters by
        extracting headers from standard_callback_dynamic_params.
        """
        from opentelemetry import trace

        from litellm.integrations.arize.arize import ArizeLogger

        standard_callback_dynamic_params: Optional[
            StandardCallbackDynamicParams
        ] = kwargs.get("standard_callback_dynamic_params")
        if not standard_callback_dynamic_params:
            return

        # Extract headers from dynamic params
        dynamic_headers = {}

        # Handle Arize headers
        dynamic_headers = ArizeLogger.construct_dynamic_arize_headers(standard_callback_dynamic_params=standard_callback_dynamic_params)

        # Only create a span processor if we have headers to use
        if len(dynamic_headers) > 0:
            from opentelemetry.sdk.trace import TracerProvider

            provider = trace.get_tracer_provider()
            if isinstance(provider, TracerProvider):
                span_processor = self._get_span_processor(
                    dynamic_headers=dynamic_headers
                )
                provider.add_span_processor(span_processor)

    def _handle_failure(self, kwargs, response_obj, start_time, end_time):
        from opentelemetry.trace import Status, StatusCode

        verbose_logger.debug(
            "OpenTelemetry Logger: Failure HandlerLogging kwargs: %s, OTEL config settings=%s",
            kwargs,
            self.config,
        )
        _parent_context, parent_otel_span = self._get_span_context(kwargs)

        # Span 1: Requst sent to litellm SDK
        span = self.tracer.start_span(
            name=self._get_span_name(kwargs),
            start_time=self._to_ns(start_time),
            context=_parent_context,
        )
        span.set_status(Status(StatusCode.ERROR))
        self.set_attributes(span, kwargs, response_obj)
        span.end(end_time=self._to_ns(end_time))

        # Create span for guardrail information
        self._create_guardrail_span(kwargs=kwargs, context=_parent_context)

        if parent_otel_span is not None:
            parent_otel_span.end(end_time=self._to_ns(datetime.now()))

    def set_tools_attributes(self, span: Span, tools):
        import json

        from litellm.proxy._types import SpanAttributes

        if not tools:
            return

        try:
            for i, tool in enumerate(tools):
                function = tool.get("function")
                if not function:
                    continue

                prefix = f"{SpanAttributes.LLM_REQUEST_FUNCTIONS.value}.{i}"
                self.safe_set_attribute(
                    span=span,
                    key=f"{prefix}.name",
                    value=function.get("name"),
                )
                self.safe_set_attribute(
                    span=span,
                    key=f"{prefix}.description",
                    value=function.get("description"),
                )
                self.safe_set_attribute(
                    span=span,
                    key=f"{prefix}.parameters",
                    value=json.dumps(function.get("parameters")),
                )
        except Exception as e:
            verbose_logger.error(
                "OpenTelemetry: Error setting tools attributes: %s", str(e)
            )
            pass

    def cast_as_primitive_value_type(self, value) -> Union[str, bool, int, float]:
        """
        Casts the value to a primitive OTEL type if it is not already a primitive type.

        OTEL supports - str, bool, int, float

        If it's not a primitive type, then it's converted to a string
        """
        if value is None:
            return ""
        if isinstance(value, (str, bool, int, float)):
            return value
        try:
            return str(value)
        except Exception:
            return ""

    @staticmethod
    def _tool_calls_kv_pair(
        tool_calls: List[ChatCompletionMessageToolCall],
    ) -> Dict[str, Any]:
        from litellm.proxy._types import SpanAttributes

        kv_pairs: Dict[str, Any] = {}
        for idx, tool_call in enumerate(tool_calls):
            _function = tool_call.get("function")
            if not _function:
                continue

            keys = Function.__annotations__.keys()
            for key in keys:
                _value = _function.get(key)
                if _value:
                    kv_pairs[
                        f"{SpanAttributes.LLM_COMPLETIONS.value}.{idx}.function_call.{key}"
                    ] = _value

        return kv_pairs

    def set_attributes(  # noqa: PLR0915
        self, span: Span, kwargs, response_obj: Optional[Any]
    ):
        try:
            if self.callback_name == "arize_phoenix":
                from litellm.integrations.arize.arize_phoenix import ArizePhoenixLogger

                ArizePhoenixLogger.set_arize_phoenix_attributes(
                    span, kwargs, response_obj
                )
                return
            elif self.callback_name == "langtrace":
                from litellm.integrations.langtrace import LangtraceAttributes

                LangtraceAttributes().set_langtrace_attributes(
                    span, kwargs, response_obj
                )
                return
            elif self.callback_name == "langfuse_otel":
                from litellm.integrations.langfuse.langfuse_otel import (
                    LangfuseOtelLogger,
                )

                LangfuseOtelLogger.set_langfuse_otel_attributes(
                    span, kwargs, response_obj
                )
                return
            from litellm.proxy._types import SpanAttributes

            optional_params = kwargs.get("optional_params", {})
            litellm_params = kwargs.get("litellm_params", {}) or {}
            standard_logging_payload: Optional[StandardLoggingPayload] = kwargs.get(
                "standard_logging_object"
            )
            if standard_logging_payload is None:
                raise ValueError("standard_logging_object not found in kwargs")

            # https://github.com/open-telemetry/semantic-conventions/blob/main/model/registry/gen-ai.yaml
            # Following Conventions here: https://github.com/open-telemetry/semantic-conventions/blob/main/docs/gen-ai/llm-spans.md
            #############################################
            ############ LLM CALL METADATA ##############
            #############################################
            metadata = standard_logging_payload["metadata"]
            for key, value in metadata.items():
                self.safe_set_attribute(
                    span=span, key="metadata.{}".format(key), value=value
                )

            #############################################
            ########## LLM Request Attributes ###########
            #############################################

            # The name of the LLM a request is being made to
            if kwargs.get("model"):
                self.safe_set_attribute(
                    span=span,
                    key=SpanAttributes.LLM_REQUEST_MODEL.value,
                    value=kwargs.get("model"),
                )

            # The LLM request type
            self.safe_set_attribute(
                span=span,
                key=SpanAttributes.LLM_REQUEST_TYPE.value,
                value=standard_logging_payload["call_type"],
            )

            # The Generative AI Provider: Azure, OpenAI, etc.
            self.safe_set_attribute(
                span=span,
                key=SpanAttributes.LLM_SYSTEM.value,
                value=litellm_params.get("custom_llm_provider", "Unknown"),
            )

            # The maximum number of tokens the LLM generates for a request.
            if optional_params.get("max_tokens"):
                self.safe_set_attribute(
                    span=span,
                    key=SpanAttributes.LLM_REQUEST_MAX_TOKENS.value,
                    value=optional_params.get("max_tokens"),
                )

            # The temperature setting for the LLM request.
            if optional_params.get("temperature"):
                self.safe_set_attribute(
                    span=span,
                    key=SpanAttributes.LLM_REQUEST_TEMPERATURE.value,
                    value=optional_params.get("temperature"),
                )

            # The top_p sampling setting for the LLM request.
            if optional_params.get("top_p"):
                self.safe_set_attribute(
                    span=span,
                    key=SpanAttributes.LLM_REQUEST_TOP_P.value,
                    value=optional_params.get("top_p"),
                )

            self.safe_set_attribute(
                span=span,
                key=SpanAttributes.LLM_IS_STREAMING.value,
                value=str(optional_params.get("stream", False)),
            )

            if optional_params.get("user"):
                self.safe_set_attribute(
                    span=span,
                    key=SpanAttributes.LLM_USER.value,
                    value=optional_params.get("user"),
                )

            # The unique identifier for the completion.
            if response_obj and response_obj.get("id"):
                self.safe_set_attribute(
                    span=span, key="gen_ai.response.id", value=response_obj.get("id")
                )

            # The model used to generate the response.
            if response_obj and response_obj.get("model"):
                self.safe_set_attribute(
                    span=span,
                    key=SpanAttributes.LLM_RESPONSE_MODEL.value,
                    value=response_obj.get("model"),
                )

            usage = response_obj and response_obj.get("usage")
            if usage:
                self.safe_set_attribute(
                    span=span,
                    key=SpanAttributes.LLM_USAGE_TOTAL_TOKENS.value,
                    value=usage.get("total_tokens"),
                )

                # The number of tokens used in the LLM response (completion).
                self.safe_set_attribute(
                    span=span,
                    key=SpanAttributes.LLM_USAGE_COMPLETION_TOKENS.value,
                    value=usage.get("completion_tokens"),
                )

                # The number of tokens used in the LLM prompt.
                self.safe_set_attribute(
                    span=span,
                    key=SpanAttributes.LLM_USAGE_PROMPT_TOKENS.value,
                    value=usage.get("prompt_tokens"),
                )

            ########################################################################
            ########## LLM Request Medssages / tools / content Attributes ###########
            #########################################################################

            if litellm.turn_off_message_logging is True:
                return
            if self.message_logging is not True:
                return

            if optional_params.get("tools"):
                tools = optional_params["tools"]
                self.set_tools_attributes(span, tools)

            if kwargs.get("messages"):
                for idx, prompt in enumerate(kwargs.get("messages")):
                    if prompt.get("role"):
                        self.safe_set_attribute(
                            span=span,
                            key=f"{SpanAttributes.LLM_PROMPTS.value}.{idx}.role",
                            value=prompt.get("role"),
                        )

                    if prompt.get("content"):
                        if not isinstance(prompt.get("content"), str):
                            prompt["content"] = str(prompt.get("content"))
                        self.safe_set_attribute(
                            span=span,
                            key=f"{SpanAttributes.LLM_PROMPTS.value}.{idx}.content",
                            value=prompt.get("content"),
                        )
            #############################################
            ########## LLM Response Attributes ##########
            #############################################
            if response_obj is not None:
                if response_obj.get("choices"):
                    for idx, choice in enumerate(response_obj.get("choices")):
                        if choice.get("finish_reason"):
                            self.safe_set_attribute(
                                span=span,
                                key=f"{SpanAttributes.LLM_COMPLETIONS.value}.{idx}.finish_reason",
                                value=choice.get("finish_reason"),
                            )
                        if choice.get("message"):
                            if choice.get("message").get("role"):
                                self.safe_set_attribute(
                                    span=span,
                                    key=f"{SpanAttributes.LLM_COMPLETIONS.value}.{idx}.role",
                                    value=choice.get("message").get("role"),
                                )
                            if choice.get("message").get("content"):
                                if not isinstance(
                                    choice.get("message").get("content"), str
                                ):
                                    choice["message"]["content"] = str(
                                        choice.get("message").get("content")
                                    )
                                self.safe_set_attribute(
                                    span=span,
                                    key=f"{SpanAttributes.LLM_COMPLETIONS.value}.{idx}.content",
                                    value=choice.get("message").get("content"),
                                )

                            message = choice.get("message")
                            tool_calls = message.get("tool_calls")
                            if tool_calls:
                                kv_pairs = OpenTelemetry._tool_calls_kv_pair(tool_calls)  # type: ignore
                                for key, value in kv_pairs.items():
                                    self.safe_set_attribute(
                                        span=span,
                                        key=key,
                                        value=value,
                                    )

        except Exception as e:
            verbose_logger.exception(
                "OpenTelemetry logging error in set_attributes %s", str(e)
            )

    def _cast_as_primitive_value_type(self, value) -> Union[str, bool, int, float]:
        """
        Casts the value to a primitive OTEL type if it is not already a primitive type.

        OTEL supports - str, bool, int, float

        If it's not a primitive type, then it's converted to a string
        """
        if value is None:
            return ""
        if isinstance(value, (str, bool, int, float)):
            return value
        try:
            return str(value)
        except Exception:
            return ""

    def safe_set_attribute(self, span: Span, key: str, value: Any):
        """
        Safely sets an attribute on the span, ensuring the value is a primitive type.
        """
        primitive_value = self._cast_as_primitive_value_type(value)
        span.set_attribute(key, primitive_value)

    def set_raw_request_attributes(self, span: Span, kwargs, response_obj):
        kwargs.get("optional_params", {})
        litellm_params = kwargs.get("litellm_params", {}) or {}
        custom_llm_provider = litellm_params.get("custom_llm_provider", "Unknown")

        _raw_response = kwargs.get("original_response")
        _additional_args = kwargs.get("additional_args", {}) or {}
        complete_input_dict = _additional_args.get("complete_input_dict")
        #############################################
        ########## LLM Request Attributes ###########
        #############################################

        # OTEL Attributes for the RAW Request to https://docs.anthropic.com/en/api/messages
        if complete_input_dict and isinstance(complete_input_dict, dict):
            for param, val in complete_input_dict.items():
                self.safe_set_attribute(
                    span=span, key=f"llm.{custom_llm_provider}.{param}", value=val
                )

        #############################################
        ########## LLM Response Attributes ##########
        #############################################
        if _raw_response and isinstance(_raw_response, str):
            # cast sr -> dict
            import json

            try:
                _raw_response = json.loads(_raw_response)
                for param, val in _raw_response.items():
                    self.safe_set_attribute(
                        span=span,
                        key=f"llm.{custom_llm_provider}.{param}",
                        value=val,
                    )
            except json.JSONDecodeError:
                verbose_logger.debug(
                    "litellm.integrations.opentelemetry.py::set_raw_request_attributes() - raw_response not json string - {}".format(
                        _raw_response
                    )
                )

                self.safe_set_attribute(
                    span=span,
                    key=f"llm.{custom_llm_provider}.stringified_raw_response",
                    value=_raw_response,
                )

    def _to_ns(self, dt):
        return int(dt.timestamp() * 1e9)

    def _get_span_name(self, kwargs):
        return LITELLM_REQUEST_SPAN_NAME

    def get_traceparent_from_header(self, headers):
        if headers is None:
            return None
        _traceparent = headers.get("traceparent", None)
        if _traceparent is None:
            return None

        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )

        propagator = TraceContextTextMapPropagator()
        carrier = {"traceparent": _traceparent}
        _parent_context = propagator.extract(carrier=carrier)

        return _parent_context

    def _get_span_context(self, kwargs):
        from opentelemetry import trace
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )

        litellm_params = kwargs.get("litellm_params", {}) or {}
        proxy_server_request = litellm_params.get("proxy_server_request", {}) or {}
        headers = proxy_server_request.get("headers", {}) or {}
        traceparent = headers.get("traceparent", None)
        _metadata = litellm_params.get("metadata", {}) or {}
        parent_otel_span = _metadata.get("litellm_parent_otel_span", None)

        """
        Two way to use parents in opentelemetry
        - using the traceparent header
        - using the parent_otel_span in the [metadata][parent_otel_span]
        """
        if parent_otel_span is not None:
            return trace.set_span_in_context(parent_otel_span), parent_otel_span

        if traceparent is None:
            return None, None
        else:
            carrier = {"traceparent": traceparent}
            return TraceContextTextMapPropagator().extract(carrier=carrier), None

    def _get_span_processor(self, dynamic_headers: Optional[dict] = None):
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as OTLPSpanExporterGRPC,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as OTLPSpanExporterHTTP,
        )
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
            SimpleSpanProcessor,
            SpanExporter,
        )

        verbose_logger.debug(
            "OpenTelemetry Logger, initializing span processor \nself.OTEL_EXPORTER: %s\nself.OTEL_ENDPOINT: %s\nself.OTEL_HEADERS: %s",
            self.OTEL_EXPORTER,
            self.OTEL_ENDPOINT,
            self.OTEL_HEADERS,
        )
        _split_otel_headers = OpenTelemetry._get_headers_dictionary(
            headers=dynamic_headers or self.OTEL_HEADERS
        )

        if hasattr(
            self.OTEL_EXPORTER, "export"
        ):  # Check if it has the export method that SpanExporter requires
            verbose_logger.debug(
                "OpenTelemetry: intiializing SpanExporter. Value of OTEL_EXPORTER: %s",
                self.OTEL_EXPORTER,
            )
            return SimpleSpanProcessor(cast(SpanExporter, self.OTEL_EXPORTER))

        if self.OTEL_EXPORTER == "console":
            verbose_logger.debug(
                "OpenTelemetry: intiializing console exporter. Value of OTEL_EXPORTER: %s",
                self.OTEL_EXPORTER,
            )
            return BatchSpanProcessor(ConsoleSpanExporter())
        elif (
            self.OTEL_EXPORTER == "otlp_http"
            or self.OTEL_EXPORTER == "http/protobuf"
            or self.OTEL_EXPORTER == "http/json"
        ):
            verbose_logger.debug(
                "OpenTelemetry: intiializing http exporter. Value of OTEL_EXPORTER: %s",
                self.OTEL_EXPORTER,
            )
            return BatchSpanProcessor(
                OTLPSpanExporterHTTP(
                    endpoint=self.OTEL_ENDPOINT, headers=_split_otel_headers
                ),
            )
        elif self.OTEL_EXPORTER == "otlp_grpc" or self.OTEL_EXPORTER == "grpc":
            verbose_logger.debug(
                "OpenTelemetry: intiializing grpc exporter. Value of OTEL_EXPORTER: %s",
                self.OTEL_EXPORTER,
            )
            return BatchSpanProcessor(
                OTLPSpanExporterGRPC(
                    endpoint=self.OTEL_ENDPOINT, headers=_split_otel_headers
                ),
            )
        else:
            verbose_logger.debug(
                "OpenTelemetry: intiializing console exporter. Value of OTEL_EXPORTER: %s",
                self.OTEL_EXPORTER,
            )
            return BatchSpanProcessor(ConsoleSpanExporter())

    @staticmethod
    def _get_headers_dictionary(headers: Optional[Union[str, dict]]) -> Dict[str, str]:
        """
        Convert a string or dictionary of headers into a dictionary of headers.
        """
        _split_otel_headers: Dict[str, str] = {}
        if headers:
            if isinstance(headers, str):
                # when passed HEADERS="x-honeycomb-team=B85YgLm96******"
                # Split only on first '=' occurrence
                parts = headers.split("=", 1)
                if len(parts) == 2:
                    _split_otel_headers = {parts[0]: parts[1]}
                else:
                    _split_otel_headers = {}
            elif isinstance(headers, dict):
                _split_otel_headers = headers
        return _split_otel_headers

    async def async_management_endpoint_success_hook(
        self,
        logging_payload: ManagementEndpointLoggingPayload,
        parent_otel_span: Optional[Span] = None,
    ):
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        _start_time_ns = 0
        _end_time_ns = 0

        start_time = logging_payload.start_time
        end_time = logging_payload.end_time

        if isinstance(start_time, float):
            _start_time_ns = int(start_time * 1e9)
        else:
            _start_time_ns = self._to_ns(start_time)

        if isinstance(end_time, float):
            _end_time_ns = int(end_time * 1e9)
        else:
            _end_time_ns = self._to_ns(end_time)

        if parent_otel_span is not None:
            _span_name = logging_payload.route
            management_endpoint_span = self.tracer.start_span(
                name=_span_name,
                context=trace.set_span_in_context(parent_otel_span),
                start_time=_start_time_ns,
            )

            _request_data = logging_payload.request_data
            if _request_data is not None:
                for key, value in _request_data.items():
                    self.safe_set_attribute(
                        span=management_endpoint_span,
                        key=f"request.{key}",
                        value=value,
                    )

            _response = logging_payload.response
            if _response is not None:
                for key, value in _response.items():
                    self.safe_set_attribute(
                        span=management_endpoint_span,
                        key=f"response.{key}",
                        value=value,
                    )

            management_endpoint_span.set_status(Status(StatusCode.OK))
            management_endpoint_span.end(end_time=_end_time_ns)

    async def async_management_endpoint_failure_hook(
        self,
        logging_payload: ManagementEndpointLoggingPayload,
        parent_otel_span: Optional[Span] = None,
    ):
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        _start_time_ns = 0
        _end_time_ns = 0

        start_time = logging_payload.start_time
        end_time = logging_payload.end_time

        if isinstance(start_time, float):
            _start_time_ns = int(int(start_time) * 1e9)
        else:
            _start_time_ns = self._to_ns(start_time)

        if isinstance(end_time, float):
            _end_time_ns = int(int(end_time) * 1e9)
        else:
            _end_time_ns = self._to_ns(end_time)

        if parent_otel_span is not None:
            _span_name = logging_payload.route
            management_endpoint_span = self.tracer.start_span(
                name=_span_name,
                context=trace.set_span_in_context(parent_otel_span),
                start_time=_start_time_ns,
            )

            _request_data = logging_payload.request_data
            if _request_data is not None:
                for key, value in _request_data.items():
                    self.safe_set_attribute(
                        span=management_endpoint_span,
                        key=f"request.{key}",
                        value=value,
                    )

            _exception = logging_payload.exception
            self.safe_set_attribute(
                span=management_endpoint_span,
                key="exception",
                value=str(_exception),
            )
            management_endpoint_span.set_status(Status(StatusCode.ERROR))
            management_endpoint_span.end(end_time=_end_time_ns)

    def create_litellm_proxy_request_started_span(
        self,
        start_time: datetime,
        headers: dict,
    ) -> Optional[Span]:
        """
        Create a span for the received proxy server request.
        """
        return self.tracer.start_span(
            name="Received Proxy Server Request",
            start_time=self._to_ns(start_time),
            context=self.get_traceparent_from_header(headers=headers),
            kind=self.span_kind.SERVER,
        )
