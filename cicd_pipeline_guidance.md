# GPOC EKS Deployment Pipeline — Implementation Guide

> Derived from: `epm-gpoc-bootstrap` (shared platform templates) + `aps-rcyc` (reference service).
> Use this when onboarding a **new service** to the EPAM GPOC Kubernetes deployment pipeline.

---

## How it fits together

```
epm-gpoc-bootstrap (GitLab project: epm-gdpl/ai-poc-service/epm-gpoc-bootstrap)
├── common-ci-templates/steps/
│   ├── aws.yml          → OIDC → AWS assume-role + EKS kubeconfig
│   ├── docker_ecr.yml   → build Docker image + push to ECR (commit SHA + latest)
│   └── helm_aws.yml     → helm diff / upgrade / destroy / diff-then-upgrade
└── helm-generic/        → published as OCI chart to ECR; renders Deployment/StatefulSet
    ├── Chart.yaml         (current version: 0.1.2; aps-rcyc uses 0.1.1)
    ├── values.yaml        (defaults)
    └── templates/         (deployment, service, ingress, hpa, statefulset, serviceaccount)

Your service repo
├── .gitlab-ci.yml              → includes bootstrap steps; defines build + deploy jobs
└── infrastructure/helm/
    └── <service>-values.yaml   → values override for helm-generic
```

---

## Step 1 — Include the shared CI templates

At the top of `.gitlab-ci.yml`:

```yaml
include:
  - project: 'epm-gdpl/ai-poc-service/epm-gpoc-bootstrap'
    ref: v1.0.2          # pin to a release tag
    file:
      - 'common-ci-templates/steps/aws.yml'
      - 'common-ci-templates/steps/docker_ecr.yml'
      - 'common-ci-templates/steps/helm_aws.yml'

stages:
  - build-images
  - deploy
```

---

## Step 2 — Build job (Docker → ECR)

```yaml
build-<service>-on-epam:
  stage: build-images
  when: manual
  extends: .docker_ecr_build
  services:
    - name: docker:24.0.7-dind
      alias: docker
  id_tokens:
    JWT_CI_TOKEN:
      aud: https://git.epam.com
  variables:
    AWS_REGION: "us-east-1"
    IMAGE_NAME: "<ecr-repo-name>"        # ECR repository name (no registry prefix)
    BUILD_CONTEXT: "<build-dir>"         # e.g. "dial_agent" or "."
    DOCKERFILE: "<build-dir>/Dockerfile"
    IMAGE_TAG: "$CI_COMMIT_SHORT_SHA"
    DOCKER_HOST: tcp://docker:2375
    DOCKER_TLS_CERTDIR: ""
```

**What `.docker_ecr_build` does:**
1. Assumes role `arn:aws:iam::711156763240:role/GitLabCI-Role-epm-gpoc` via OIDC JWT.
2. Logs into ECR registry `711156763240.dkr.ecr.us-east-1.amazonaws.com`.
3. Builds the image and pushes **two tags**: `$CI_COMMIT_SHORT_SHA` and `latest`.

**Important:** `DOCKER_TLS_CERTDIR: ""` and `DOCKER_HOST: tcp://docker:2375` are required for DinD — do not change them (comment in template: "3h spent on this, do not touch").

---

## Step 3 — Helm values file

Create `infrastructure/helm/<service>-values.yaml` (override of `helm-generic/values.yaml`):

```yaml
---
# Generic Helm chart values

workload:
  kind: "Deployment"   # or "StatefulSet"

replicaCount: 1

autoscaling:
  enabled: false
  minReplicas: 1
  maxReplicas: 5
  metrics: []

image:
  repository: 711156763240.dkr.ecr.us-east-1.amazonaws.com/<ecr-repo-name>
  tag: "latest"        # overridden at deploy time by CI via --set image.tag=<sha>
  pullPolicy: Always

env:
  LOG_LEVEL: "DEBUG"
  # Add all service-specific env vars here (plain string values only)
  # Secrets should use K8s Secrets / ExternalSecrets, not plain env here

ports:
  - containerPort: <PORT>
    name: http
    protocol: TCP

resources: {}          # set limits/requests for production

livenessProbe:
  exec:
    command: ["curl", "-fsS", "http://localhost:<PORT>/health"]
  initialDelaySeconds: 10
  periodSeconds: 30
  timeoutSeconds: 3
  failureThreshold: 3

readinessProbe:
  exec:
    command: ["curl", "-fsS", "http://localhost:<PORT>/health"]
  initialDelaySeconds: 10
  periodSeconds: 30
  timeoutSeconds: 3
  failureThreshold: 3

strategy:
  rollingUpdate:
    maxSurge: 25%
    maxUnavailable: 25%
  type: RollingUpdate

serviceAccount:
  create: false
  annotations: {}
  name: ""

service:
  type: ClusterIP
  port: 80
  protocol: TCP
  name: http
  annotations: {}
  loadBalancerSourceRanges: []

ingress:
  enabled: false
  className: "nginx-ingress"

nodeSelector: {}
tolerations: []
affinity: {}
imagePullSecrets: []
```

**Key points:**
- `image.tag: "latest"` is just the fallback — the CI script sets `--set image.tag=<sha>` at deploy time.
- `env` values must be plain strings. The template renders them as `value: "..."` using `quote`.
- `ingress.enabled: false` is the default for internal services; set to `true` + configure `hosts` for externally reachable services.

---

## Step 4 — Deploy job (Helm → EKS)

```yaml
deploy-<service>-on-epam:
  stage: deploy
  when: manual
  extends: .helm_aws_diff_then_upgrade
  id_tokens:
    JWT_CI_TOKEN:
      aud: https://git.epam.com
  variables:
    HELM_RELEASE_NAME: <release-name>       # e.g. "my-service-agent"
    HELM_NS_NAME: <namespace>               # e.g. "dialv2" for DIAL stack services
    HELM_CHART: oci://711156763240.dkr.ecr.us-east-1.amazonaws.com/helm/helm-generic:0.1.1
    HELM_VALUES_FILES: "infrastructure/helm/<service>-values.yaml"
    IMAGE_NAME: <ecr-repo-name>             # same as in build job
    OCI_REGISTRY: 711156763240.dkr.ecr.us-east-1.amazonaws.com
```

**What `.helm_aws_diff_then_upgrade` does:**
1. Assumes the OIDC role and writes `~/.kube/config` for cluster `epam-gpoc-eks` in `us-east-1`.
2. Logs into ECR for Helm OCI chart pull.
3. Resolves the **effective image tag**:
   - If `$CI_COMMIT_SHORT_SHA` exists as a tag in ECR → uses it.
   - Otherwise falls back to `latest`.
4. Runs `helm diff upgrade ... --allow-unreleased` (shows what would change, non-fatal).
5. Sanity-checks cluster auth (`kubectl auth can-i`).
6. Runs `helm upgrade --install ... --create-namespace --wait --timeout 15m`.

---

## Platform constants (do not change)

| Constant | Value |
|---|---|
| AWS Account ID | `711156763240` |
| AWS Region | `us-east-1` |
| ECR Registry | `711156763240.dkr.ecr.us-east-1.amazonaws.com` |
| EKS Cluster | `epam-gpoc-eks` |
| GitLab IAM Role | `arn:aws:iam::711156763240:role/GitLabCI-Role-epm-gpoc` |
| JWT audience | `https://git.epam.com` |
| helm-generic OCI | `oci://711156763240.dkr.ecr.us-east-1.amazonaws.com/helm/helm-generic` |
| helm-generic version in use | `0.1.1` (latest chart version is `0.1.2`) |
| CI runner image | `registry.git.epam.com/epm-gdpl/ai-poc-service/epm-gpoc-bootstrap/infra-utils:latest` |

---

## Required GitLab CI variables (set at project level)

| Variable | Notes |
|---|---|
| `SSH_PRIVATE_KEY` | Base64-encoded RSA private key (for RCYC SSH path only) |

OIDC auth is fully managed by `JWT_CI_TOKEN` + `id_tokens` — no static AWS credentials needed.

---

## Available job template variants

| Template | Use case |
|---|---|
| `.helm_aws_diff` | Show diff only, no apply |
| `.helm_aws_upgrade` | Apply only (no diff preview) |
| `.helm_aws_diff_then_upgrade` | **Recommended**: shows diff, then applies |
| `.helm_aws_destroy` | `helm uninstall` |
| `.docker_ecr_build` | Build + push image to ECR |
| `.kubeconfig_from_eks` | Just configure kubectl (used as `before_script` ref) |
| `.aws_assume_role` | Just assume role (used as `before_script` ref) |

---

## Namespace convention for DIAL stack services

Services that call DIAL Core use namespace `dialv2`. Inside the cluster the Core is reachable at:
```
http://dialv2-core.dialv2.svc.cluster.local
```
Set `DIAL_API_HOST` to this value in `env:` of the Helm values file.

---

## helm-generic template capabilities summary

The chart (`helm-generic`) renders these Kubernetes resources based on values:

| Resource | Condition |
|---|---|
| `Deployment` | `workload.kind: "Deployment"` (default) |
| `StatefulSet` | `workload.kind: "StatefulSet"` |
| `Service` | always |
| `Ingress` | `ingress.enabled: true` |
| `HPA` | `autoscaling.enabled: true` |
| `ServiceAccount` | `serviceAccount.create: true` |

`env` values are rendered as plain container env vars. Secrets must be injected separately (ExternalSecrets, mounted secrets, etc.) — the generic chart has no native secret injection.

---

## Reference: aps-rcyc as the canonical example

- Build job: `build-rcyc-callsense-on-epam` (`.gitlab-ci.yml:53`)
- Deploy job: `deploy-callsense-on-epam` (`.gitlab-ci.yml:91`)
- Helm values: `infrastructure/helm/callsense-values.yaml`
- ECR repo: `aps-rcyc`
- Release name: `callsense-agent`
- Namespace: `dialv2`
- Port: `5000`
- Health check: `curl -fsS http://localhost:5000/health`
