# Plays real matches on Fargate (docs/architecture.md). platform/prototype/run_batch.py
# starts one match task per match with RunTask. Each one runs the controller, starts its two bots
# (matches_bots.tf, or matches_bot_hosts.tf for bots on EC2), and stores its replay in S3 and its
# result in Postgres. While no match runs, only the database and the VPC endpoints cost anything.
locals {
  matches_name = "${local.name_prefix}-${local.environment}-matches"
}

module "match_ecr" {
  source = "../../modules/ecr"
  name   = "${local.name_prefix}/match"
}

resource "aws_ecs_cluster" "matches" {
  name = local.matches_name
}

# One address per running match task: two /22s, about 2,000 addresses, so 1,000 matches at once
# fit with room to spare. Their bots have subnets of their own.
resource "aws_subnet" "match_tasks" {
  for_each          = { a = "10.0.4.0/22", b = "10.0.12.0/22" }
  vpc_id            = module.dummy_vpc.vpc_id
  cidr_block        = each.value
  availability_zone = "${local.aws_region}${each.key}"
  tags              = { Name = "${local.matches_name}-tasks-${each.key}" }
}

moved {
  from = aws_subnet.match_tasks
  to   = aws_subnet.match_tasks["a"]
}

resource "aws_route_table_association" "match_tasks" {
  for_each       = aws_subnet.match_tasks
  subnet_id      = each.value.id
  route_table_id = module.dummy_vpc.public_route_table_id
}

moved {
  from = aws_route_table_association.match_tasks
  to   = aws_route_table_association.match_tasks["a"]
}

# RDS needs subnets in two availability zones. No route to the internet.
resource "aws_subnet" "match_db" {
  for_each          = { a = "10.0.8.0/24", b = "10.0.9.0/24" }
  vpc_id            = module.dummy_vpc.vpc_id
  cidr_block        = each.value
  availability_zone = "${local.aws_region}${each.key}"
  tags              = { Name = "${local.matches_name}-db-${each.key}" }
}

resource "aws_db_subnet_group" "matches" {
  name       = local.matches_name
  subnet_ids = [for subnet in aws_subnet.match_db : subnet.id]
}

resource "aws_security_group" "match_tasks" {
  name        = "${local.matches_name}-tasks"
  description = "Match tasks: no inbound; outbound for ECR, S3, CloudWatch and the database"
  vpc_id      = module.dummy_vpc.vpc_id

  egress {
    description = "ECR, S3 and CloudWatch through the internet gateway; no NAT in dev"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "The database"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [for subnet in aws_subnet.match_db : subnet.cidr_block]
  }
}

resource "aws_security_group" "match_db" {
  name        = "${local.matches_name}-db"
  description = "Match results database: only from match tasks"
  vpc_id      = module.dummy_vpc.vpc_id

  ingress {
    description     = "Postgres from match tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.match_tasks.id]
  }
}

resource "aws_db_instance" "matches" {
  identifier                  = local.matches_name
  engine                      = "postgres"
  engine_version              = "18"
  instance_class              = "db.t4g.micro"
  allocated_storage           = 20
  storage_type                = "gp3"
  storage_encrypted           = true
  db_name                     = "simulation"
  username                    = "simulation_admin"
  manage_master_user_password = true
  db_subnet_group_name        = aws_db_subnet_group.matches.name
  vpc_security_group_ids      = [aws_security_group.match_db.id]
  publicly_accessible         = false
  backup_retention_period     = 1
  skip_final_snapshot         = true
  apply_immediately           = true
}

resource "aws_s3_bucket" "match_replays" {
  bucket = "${local.matches_name}-replays-${local.account_id}"
}

resource "aws_s3_bucket_public_access_block" "match_replays" {
  bucket                  = aws_s3_bucket.match_replays.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "match_replays" {
  bucket = aws_s3_bucket.match_replays.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_cloudwatch_log_group" "matches" {
  name = "/ecs/${local.matches_name}"
  # The monitoring dashboard reads past batches from here, so they stay for a year.
  retention_in_days = 365
}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# What ECS needs to start a task: pull the image, write logs, read the database password.
resource "aws_iam_role" "match_execution" {
  name               = "${local.matches_name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "match_execution" {
  role       = aws_iam_role.match_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "match_execution_secret" {
  name = "read-database-password"
  role = aws_iam_role.match_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_db_instance.matches.master_user_secret[0].secret_arn
    }]
  })
}

# What the match itself may do: only upload replays.
resource "aws_iam_role" "match_task" {
  name               = "${local.matches_name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "match_task_replays" {
  name = "upload-replays"
  role = aws_iam_role.match_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "s3:PutObject"
      Resource = "${aws_s3_bucket.match_replays.arn}/batches/*"
    }]
  })
}

# The image tag is a starting point: run_batch.py registers revisions for the tag it's given.
resource "aws_ecs_task_definition" "match" {
  family                   = local.matches_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.match_execution.arn
  task_role_arn            = aws_iam_role.match_task.arn
  skip_destroy             = true

  container_definitions = jsonencode([{
    name      = "match"
    image     = "${module.match_ecr.repository_url}:initial"
    essential = true
    environment = [
      { name = "DB_HOST", value = aws_db_instance.matches.address },
      { name = "DB_NAME", value = aws_db_instance.matches.db_name },
      { name = "DB_USER", value = aws_db_instance.matches.username },
      { name = "REPLAY_BUCKET", value = aws_s3_bucket.match_replays.bucket },
    ]
    secrets = [{
      name      = "DB_PASSWORD"
      valueFrom = "${aws_db_instance.matches.master_user_secret[0].secret_arn}:password::"
    }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.matches.name
        awslogs-region        = local.aws_region
        awslogs-stream-prefix = "match"
      }
    }
  }])
}
