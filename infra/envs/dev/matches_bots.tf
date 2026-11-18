# Each bot of a match runs in its own Fargate task, separate from the controller and from the other
# bot (docs/architecture.md). The match task starts them with RunTask.
#
# Bots have no route to the internet, no task role and no secrets. They can reach their controller
# on port 9000. ECR, S3 and CloudWatch Logs are reachable because Fargate pulls the image and ships
# the logs over the task's own network interface.
locals {
  match_bots_name = "${local.matches_name}-bot"
  # Our account only, so a bot can't use the endpoints with credentials of its own to send data
  # to another account's logs or registry.
  match_endpoint_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = "*"
      Resource  = "*"
      Condition = { StringEquals = { "aws:PrincipalAccount" = local.account_id } }
    }]
  })
}

module "match_bot_ecr" {
  source = "../../modules/ecr"
  name   = "${local.name_prefix}/match-bot"
}

# One /20 per availability zone: 4,000 addresses each. A bot on EC2 takes two (its machine and its
# task), so one zone alone holds the bots of 1,000 matches. Spot capacity comes from all three.
resource "aws_subnet" "match_bots" {
  for_each          = { a = "10.0.16.0/20", b = "10.0.32.0/20", c = "10.0.48.0/20" }
  vpc_id            = module.dummy_vpc.vpc_id
  cidr_block        = each.value
  availability_zone = "${local.aws_region}${each.key}"
  tags              = { Name = "${local.matches_name}-bots-${each.key}" }
}

moved {
  from = aws_subnet.match_bots
  to   = aws_subnet.match_bots["a"]
}

# Only the VPC itself and the S3 endpoint below: no internet gateway.
resource "aws_route_table" "match_bots" {
  vpc_id = module.dummy_vpc.vpc_id
  tags   = { Name = "${local.matches_name}-bots" }
}

resource "aws_route_table_association" "match_bots" {
  for_each       = aws_subnet.match_bots
  subnet_id      = each.value.id
  route_table_id = aws_route_table.match_bots.id
}

moved {
  from = aws_route_table_association.match_bots
  to   = aws_route_table_association.match_bots["a"]
}

resource "aws_security_group" "match_bots" {
  name        = "${local.matches_name}-bots"
  description = "Bot tasks: no inbound; outbound to controllers, the endpoints and S3 for image layers"
  vpc_id      = module.dummy_vpc.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "match_bots_to_controllers" {
  security_group_id            = aws_security_group.match_bots.id
  description                  = "The controller"
  ip_protocol                  = "tcp"
  from_port                    = 9000
  to_port                      = 9000
  referenced_security_group_id = aws_security_group.match_tasks.id
}

resource "aws_vpc_security_group_egress_rule" "match_bots_to_endpoints" {
  security_group_id            = aws_security_group.match_bots.id
  description                  = "ECR and CloudWatch Logs endpoints, for the image and the logs"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.match_endpoints.id
}

resource "aws_vpc_security_group_egress_rule" "match_bots_to_s3" {
  security_group_id = aws_security_group.match_bots.id
  description       = "S3 endpoint, for image layers"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = aws_vpc_endpoint.match_s3.prefix_list_id
}

resource "aws_vpc_security_group_ingress_rule" "match_tasks_from_bots" {
  security_group_id            = aws_security_group.match_tasks.id
  description                  = "Bots connecting to their controller"
  ip_protocol                  = "tcp"
  from_port                    = 9000
  to_port                      = 9000
  referenced_security_group_id = aws_security_group.match_bots.id
}

resource "aws_security_group" "match_endpoints" {
  name        = "${local.matches_name}-endpoints"
  description = "ECR and CloudWatch Logs endpoints: from match and bot tasks"
  vpc_id      = module.dummy_vpc.vpc_id
}

# Private DNS sends the whole VPC's ECR and logs traffic to the endpoints, so match tasks need
# this rule as well, although they could reach those services through the internet gateway.
resource "aws_vpc_security_group_ingress_rule" "match_endpoints" {
  for_each = {
    bots  = aws_security_group.match_bots.id
    tasks = aws_security_group.match_tasks.id
  }
  security_group_id            = aws_security_group.match_endpoints.id
  description                  = "HTTPS from ${each.key}"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = each.value
}

# In zone a only: each zone more would cost another $0.012/h per endpoint, around the clock. Bots in
# zones b and c reach them across zones, for a few MB each. Image layers come through the S3
# gateway endpoint, which serves every zone.
resource "aws_vpc_endpoint" "match_interface" {
  for_each            = toset(["ecr.api", "ecr.dkr", "logs"])
  vpc_id              = module.dummy_vpc.vpc_id
  service_name        = "com.amazonaws.${local.aws_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.match_bots["a"].id]
  security_group_ids  = [aws_security_group.match_endpoints.id]
  private_dns_enabled = true
  policy              = local.match_endpoint_policy
  tags                = { Name = "${local.matches_name}-${each.key}" }
}

# Limited to the bucket that holds ECR's image layers, so S3 can't carry anything out either.
resource "aws_vpc_endpoint" "match_s3" {
  vpc_id            = module.dummy_vpc.vpc_id
  service_name      = "com.amazonaws.${local.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.match_bots.id]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "arn:aws:s3:::prod-${local.aws_region}-starport-layer-bucket/*"
    }]
  })
  tags = { Name = "${local.matches_name}-s3" }
}

# What ECS needs to start a bot: pull the image and write logs. Unlike the match task's execution
# role, this one can't read the database password.
resource "aws_iam_role" "match_bot_execution" {
  name               = "${local.match_bots_name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "match_bot_execution" {
  role       = aws_iam_role.match_bot_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The match task starts its own bots.
resource "aws_iam_role_policy" "match_task_bots" {
  name = "start-bots"
  role = aws_iam_role.match_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = "ecs:RunTask"
        Resource = [
          "arn:aws:ecs:${local.aws_region}:${local.account_id}:task-definition/${local.match_bots_name}:*",
          "arn:aws:ecs:${local.aws_region}:${local.account_id}:task-definition/${local.match_bots_name}-ec2:*",
        ]
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.matches.arn } }
      },
      {
        # To drain a bot's EC2 machine as soon as the bot is placed (matches_bot_hosts.tf).
        Effect = "Allow"
        Action = ["ecs:DescribeTasks", "ecs:UpdateContainerInstancesState"]
        Resource = [
          "arn:aws:ecs:${local.aws_region}:${local.account_id}:task/${aws_ecs_cluster.matches.name}/*",
          "arn:aws:ecs:${local.aws_region}:${local.account_id}:container-instance/${aws_ecs_cluster.matches.name}/*",
        ]
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.matches.arn } }
      },
      {
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = aws_iam_role.match_bot_execution.arn
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
    ]
  })
}

# No task role: a bot has no AWS credentials at all. The image tag is a starting point, like the
# match task's: run_batch.py registers revisions for the tag it's given.
resource "aws_ecs_task_definition" "match_bot" {
  family                   = local.match_bots_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.match_bot_execution.arn
  skip_destroy             = true

  container_definitions = jsonencode([{
    name                   = "bot"
    image                  = "${module.match_bot_ecr.repository_url}:initial"
    essential              = true
    readonlyRootFilesystem = true
    # `add` as AWS stores it: without it, every plan would replace the task definition.
    linuxParameters = { capabilities = { add = [], drop = ["ALL"] } }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.matches.name
        awslogs-region        = local.aws_region
        awslogs-stream-prefix = "bot"
      }
    }
  }])
}
