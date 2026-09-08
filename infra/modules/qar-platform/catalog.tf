# ---------------------------------------------------------------------------
# Glue databases, one per medallion layer.
# ---------------------------------------------------------------------------
resource "aws_glue_catalog_database" "bronze" {
  name        = "${var.project}_bronze"
  description = "Raw segments and the file registry."
}

resource "aws_glue_catalog_database" "silver" {
  name        = "${var.project}_silver"
  description = "Decoded parameters at native rates, canonical column names."
}

resource "aws_glue_catalog_database" "gold" {
  name        = "${var.project}_gold"
  description = "Phases, events, derived parameters, fleet aggregates."
}

# ---------------------------------------------------------------------------
# Lake Formation. This is FR-8's enforcement point.
# ---------------------------------------------------------------------------
resource "aws_lakeformation_data_lake_settings" "this" {
  admins = [data.aws_caller_identity.current.arn]

  # Turn off the IAM-only shortcut so Lake Formation actually governs.
  create_database_default_permissions {}
  create_table_default_permissions {}
}

resource "aws_lakeformation_resource" "lakehouse" {
  arn = aws_s3_bucket.lakehouse.arn
}

# The decode job needs full write access to Silver.
resource "aws_lakeformation_permissions" "decoder_silver" {
  principal   = aws_iam_role.decode_job.arn
  permissions = ["ALL"]

  database {
    name = aws_glue_catalog_database.silver.name
  }

  depends_on = [aws_lakeformation_data_lake_settings.this]
}

# Analysts get the database, but see it only through the engine.
resource "aws_lakeformation_permissions" "analyst_silver" {
  principal   = aws_iam_role.analyst.arn
  permissions = ["DESCRIBE"]

  database {
    name = aws_glue_catalog_database.silver.name
  }

  depends_on = [aws_lakeformation_data_lake_settings.this]
}

resource "aws_lakeformation_permissions" "analyst_gold" {
  principal   = aws_iam_role.analyst.arn
  permissions = ["DESCRIBE"]

  database {
    name = aws_glue_catalog_database.gold.name
  }

  depends_on = [aws_lakeformation_data_lake_settings.this]
}

# ---------------------------------------------------------------------------
# Column-level grant. This is the concrete FR-8 control: the analyst role
# is granted every column EXCEPT the restricted ones, and the exclusion is
# applied by the engine at query planning time.
#
# Inert until silver_flights_table is set, because the table must exist.
# ---------------------------------------------------------------------------
resource "aws_lakeformation_permissions" "analyst_columns" {
  count = var.silver_flights_table == "" ? 0 : 1

  principal   = aws_iam_role.analyst.arn
  permissions = ["SELECT"]

  table_with_columns {
    database_name         = aws_glue_catalog_database.silver.name
    name                  = var.silver_flights_table
    excluded_column_names = var.restricted_columns
  }

  depends_on = [aws_lakeformation_data_lake_settings.this]
}
