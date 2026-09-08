# 0001 — AWS Batch on Fargate, not Kubernetes

**Status:** accepted, revisit if the query engine decision changes

## Context
The proposal budgets EKS. A colleague's deck proposed two EKS variants:
Batch-on-EKS, and KEDA plus Karpenter.

## Decision
AWS Batch on Fargate. No cluster.

## Reasoning
Decode is one file, one container, no dependencies, no service discovery,
nothing long-running. It uses almost nothing Kubernetes offers.

The decisive point is second-order: a cluster would have been justified by
the query service, because self-managed Trino has to live somewhere. With
Athena there is no long-running service anywhere in the platform, so there
is nothing to put in a cluster.

Fargate scales to zero between arrival banks. A cluster sized for peak
decode bills through the night.

## Consequences
- Standing cost near zero; higher per-unit compute cost than EC2
- Cold start of a minute or two, acceptable since nobody waits on a decode
- Switch to EC2 Spot in the same compute environment when volume justifies it

## Revisit if
- EKS already exists in the organisation for other workloads
- Trino wins the query engine decision
- A portability mandate appears
