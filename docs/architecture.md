# bedrock-api Architecture

## Overview

A serverless AWS proxy that authenticates bearer tokens, enforces per-token limits, and forwards requests to AWS Bedrock. All resource names are prefixed `bedrock-api-`.

---

## System Overview

```mermaid
graph LR
    Client["Client"]
    APIGW["API Gateway<br/>bedrock-api-http"]
    Lambda["Lambda<br/>bedrock-api-proxy"]
    DynamoDB["DynamoDB<br/>3 tables"]
    Bedrock["AWS Bedrock Runtime<br/>Converse + InvokeModel APIs"]
    CloudWatch["CloudWatch<br/>logs · metrics · alarms"]

    Client -->|HTTPS| APIGW
    APIGW -->|AWS_PROXY| Lambda
    Lambda <-->|auth · usage · rate limit| DynamoDB
    Lambda -->|routed by URL path| Bedrock
    Lambda -->|JSON logs| CloudWatch
    APIGW -->|access logs| CloudWatch

    classDef bedrockStyle fill:#fde68a,stroke:#d97706,color:#78350f
    classDef clientStyle fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a
    class Bedrock bedrockStyle
    class Client clientStyle
```

---

## Infrastructure Layout

```mermaid
graph TB
    Client["Client<br/>(Bearer bk_&lt;id&gt;.&lt;secret&gt;)"]

    subgraph APIGW["API Gateway HTTP API — bedrock-api-http"]
        Stage["$default stage<br/>throttle: 20 rps / 40 burst"]
        RouteProxy["POST /model/{proxy+}"]
        RouteUsage["GET /usage"]
        RouteOther["any other path/method → 404"]
    end

    subgraph Lambda["Lambda — bedrock-api-proxy"]
        Handler["handler.handler<br/>Python 3.12 · 512 MB · 60s · concurrency 50"]
    end

    subgraph DynamoDB["DynamoDB (PAY_PER_REQUEST)"]
        Tokens["bedrock-api-tokens<br/>PK: token_id<br/>GSI: owner-index"]
        Usage["bedrock-api-usage<br/>PK: token_id · SK: period"]
        RateLimit["bedrock-api-rate-limit<br/>PK: token_id · SK: window_second<br/>TTL: ttl"]
    end

    Bedrock["AWS Bedrock Runtime<br/>/converse → Converse API<br/>/invoke → InvokeModel API"]

    subgraph CW["CloudWatch"]
        LogLambda["/aws/lambda/bedrock-api-proxy"]
        LogAPI["/aws/apigateway/bedrock-api-http"]
        Alarm["Alarm: bedrock-api-pricing-fallback-high"]
    end

    Client -->|HTTPS| Stage
    Stage --> RouteProxy
    Stage --> RouteUsage
    Stage --> RouteOther
    RouteProxy -->|AWS_PROXY integration| Handler
    RouteUsage -->|AWS_PROXY integration| Handler
    Handler -->|GetItem| Tokens
    Handler -->|GetItem + UpdateItem| Usage
    Handler -->|UpdateItem| RateLimit
    Handler -->|routed by URL path| Bedrock
    Handler -->|JSON logs| LogLambda
    Stage -->|access logs| LogAPI
    LogLambda -->|metric filters| Alarm

    classDef bedrockStyle fill:#fde68a,stroke:#d97706,color:#78350f
    classDef clientStyle fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a
    class Bedrock bedrockStyle
    class Client clientStyle
```

---

## Request Pipeline

```mermaid
sequenceDiagram
    participant C as Client
    participant GW as API Gateway<br/>bedrock-api-http
    participant L as Lambda<br/>bedrock-api-proxy
    participant T as DynamoDB<br/>bedrock-api-tokens
    participant U as DynamoDB<br/>bedrock-api-usage
    participant R as DynamoDB<br/>bedrock-api-rate-limit
    participant B as Bedrock Runtime

    C->>GW: POST /model/{modelId}/converse<br/>Authorization: Bearer bk_<id>.<secret>
    GW->>L: invoke (payload format 2.0)

    Note over L: 1. parse bearer token (header only)
    L->>T: GetItem {token_id}
    Note over L: 2–4. revoked check + SHA-256 verify
    L->>R: UpdateItem conditional ADD (window_second)
    Note over L: 5. per-second rate limit → 429
    L->>U: GetItem {token_id, period}
    Note over L: 6–7. monthly request quota + USD budget → 429
    Note over L: 8. input token cap heuristic → 413
    Note over L: 9. model allowlist check → 403
    L->>B: Converse API (/converse) or InvokeModel API (/invoke)
    B-->>L: response + token usage metadata
    Note over L: compute USD-micros (integer, no floats)
    L->>U: UpdateItem ADD counters (post-flight)
    L-->>GW: 200 + Bedrock response body
    GW-->>C: 200
```

---

## DynamoDB Table Schemas

```mermaid
erDiagram
    TOKENS {
        string token_id PK
        string owner
        string created_at
        string status
        string secret_hash
        string allowed_models
        number limit_monthly_requests
        number limit_monthly_usd_micros
        number limit_max_input_tokens
        number limit_max_output_tokens
        number limit_rps
        string pricing_mode
    }
    USAGE {
        string token_id PK
        string period "sort key"
        number requests
        number input_tokens
        number output_tokens
        number cache_read_input_tokens
        number cache_write_input_tokens
        number usd_micros
    }
    RATE_LIMIT {
        string token_id PK
        number window_second "sort key"
        number request_count
        number ttl
    }
    TOKENS ||--o{ USAGE : "token_id"
    TOKENS ||--o{ RATE_LIMIT : "token_id"
```

---

## CloudWatch Observability

```mermaid
graph LR
    Logs["/aws/lambda/bedrock-api-proxy"]

    subgraph Filters["Log Metric Filters → namespace: bedrock-api/proxy"]
        F1["bedrock-api-request-complete-count<br/>→ RequestCompleteCount"]
        F2["bedrock-api-pricing-fallback-count<br/>→ PricingFallbackCount"]
        F3["bedrock-api-pricing-mode-invalid<br/>→ PricingModeInvalidCount"]
        F4["bedrock-api-pricing-mode-on-demand-count<br/>→ PricingModeOnDemandCount"]
        F5["bedrock-api-pricing-mode-batch-count<br/>→ PricingModeBatchCount"]
    end

    Alarm["Alarm: bedrock-api-pricing-fallback-high<br/>fires when fallbacks &gt; max(1% of requests, 100)<br/>in any 15-minute window"]

    Logs --> F1
    Logs --> F2
    Logs --> F3
    Logs --> F4
    Logs --> F5
    F1 --> Alarm
    F2 --> Alarm
```
