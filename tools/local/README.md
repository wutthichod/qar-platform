# Local stack

Docker Compose standing in for the cloud services, so decoder work needs no
AWS account.

| Production | Local | Fidelity |
|------------|-------|----------|
| S3 | MinIO | High, same API |
| SQS | ElasticMQ | High, same API |
| Glue Data Catalog | Iceberg REST catalog | High, same role |
| Athena | Trino | Very high -- Athena's engine is Trino |
| Lake Formation | nothing | **Cannot be demonstrated** |
| AWS Batch | a worker loop | Pattern only |

The access-control layer has no local equivalent. Anything proved here says
nothing about FR-8.
