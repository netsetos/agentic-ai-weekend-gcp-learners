"""gen_ai spans - what ran, not what was said."""
import os

from opentelemetry.instrumentation.google_genai import GoogleGenAiSdkInstrumentor

# THE DEFAULT IS ALREADY SAFE, and that is the part worth knowing.
# opentelemetry-util-genai's get_content_capturing_mode() returns NO_CONTENT
# when this variable is unset, and falls back to NO_CONTENT on an invalid
# value too. So you do not have to remember to switch prompt capture off.
#
# What you have to know is what switching it ON does: SPAN_AND_EVENT puts the
# user's prompt and the model's completion into your traces, where anyone with
# trace access can read them - which for DocuMind means a support engineer can
# read a customer's HR documents by opening Cloud Trace.
#
# Setting it explicitly is still worth doing, because "unset" and "deliberately
# off" look identical in a config review and only one of them survives someone
# tidying up.
os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
                      "NO_CONTENT")

GoogleGenAiSdkInstrumentor().instrument()

# What you get on the span WITHOUT content: the operation, the model, token
# counts, latency, finish reason, and any error - which is everything you need
# to answer "why was that slow" and "why did that cost so much", and nothing
# you need to answer "what did they ask", which is not your question.
