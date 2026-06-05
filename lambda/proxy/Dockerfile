# Base image: python:3.12-slim (NOT public.ecr.aws/lambda/python:3.12, which has an ENTRYPOINT
# that swallows CMD — uvicorn would never start). This matches the official LWA FastAPI
# streaming example.
#
# LWA tag: 0.9.1
# Pre-build check: docker manifest inspect public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1
# (or check gallery.ecr.aws/awsguru/aws-lambda-adapter) to confirm the tag exists.
#
# Build for Lambda x86_64:
#   docker buildx build --platform linux/amd64 --provenance=false -t <ecr_uri>:latest .
FROM python:3.12-slim

# Embed the Lambda Web Adapter binary (enables streaming pass-through via Function URL).
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /var/task

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# LWA environment variables:
#   PORT / AWS_LWA_PORT     — port uvicorn listens on (LWA forwards to this)
#   AWS_LWA_INVOKE_MODE     — RESPONSE_STREAM enables streaming pass-through
#   AWS_LWA_READINESS_CHECK_PATH — LWA pings GET /health before accepting traffic
ENV PORT=8080 \
    AWS_LWA_PORT=8080 \
    AWS_LWA_INVOKE_MODE=RESPONSE_STREAM \
    AWS_LWA_READINESS_CHECK_PATH=/health

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
