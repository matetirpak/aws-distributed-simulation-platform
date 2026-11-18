# Bots on EC2 instead of Fargate: one machine with one vCPU per bot, in the bots' private subnets
# (docs/architecture.md). `run_batch.py start --bots ec2` uses it. With no tasks the groups
# have no instances, so they cost nothing but the three ECS endpoints.
#
# Two groups, each behind an ECS capacity provider: Spot, which run_batch fills first, and
# on-demand for what Spot can't supply. A bot task that waits for a machine starts one, and idle
# machines go away after about 15 minutes.
#
# A machine runs one bot in its life. The bot's starter drains it as soon as the bot is placed, and
# bot_host/one-bot.sh shuts it down after the bot, which terminates it.
locals {
  match_bot_hosts_name = "${local.matches_name}-bot-hosts"
  # One bot per machine, so these are the numbers of bots at once. Each is the account's limit in
  # this region: Running On-Demand Standard instances (96 vCPU) and All Standard Spot Instance
  # Requests (2,050 vCPU). Raise a quota and its group together.
  match_bot_hosts_max      = 96
  match_bot_hosts_spot_max = 2050
  # Spot draws on these: all one vCPU with the same CPU (AMD EPYC 9005, up to 4.5 GHz), so every
  # bot gets the same speed. They differ only in memory (2, 4 and 8 GiB), and a bot gets 1.5 GiB.
  match_bot_hosts_spot_types = ["c8a.medium", "m8a.medium", "r8a.medium"]
  # ECS-optimized Amazon Linux 2023, x86_64: al2023-ami-ecs-hvm-2023.0.20261031-kernel-6.1-x86_64.
  # Pinned, so a new AMI is a reviewed change. The latest:
  #   aws ssm get-parameter --name /aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id
  match_bot_hosts_ami = "ami-0d6f244030786337b"
}

resource "aws_security_group" "match_bot_hosts" {
  name        = local.match_bot_hosts_name
  description = "Bot machines: no inbound; outbound to the endpoints and S3 for image layers"
  vpc_id      = module.dummy_vpc.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "match_bot_hosts" {
  for_each = {
    "ECR and CloudWatch Logs endpoints" = aws_security_group.match_endpoints.id
    "ECS endpoints"                     = aws_security_group.match_ecs_endpoints.id
  }
  security_group_id            = aws_security_group.match_bot_hosts.id
  description                  = each.key
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = each.value
}

resource "aws_vpc_security_group_egress_rule" "match_bot_hosts_to_s3" {
  security_group_id = aws_security_group.match_bot_hosts.id
  description       = "S3 endpoint, for image layers"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = aws_vpc_endpoint.match_s3.prefix_list_id
}

resource "aws_vpc_security_group_ingress_rule" "match_endpoints_from_bot_hosts" {
  security_group_id            = aws_security_group.match_endpoints.id
  description                  = "HTTPS from bot machines"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.match_bot_hosts.id
}

# A group of their own, so bot containers can't reach the ECS API at all. Match tasks need it:
# private DNS sends their RunTask calls here as well.
resource "aws_security_group" "match_ecs_endpoints" {
  name        = "${local.matches_name}-ecs-endpoints"
  description = "ECS endpoints: from bot machines and match tasks"
  vpc_id      = module.dummy_vpc.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "match_ecs_endpoints" {
  for_each = {
    bot_hosts = aws_security_group.match_bot_hosts.id
    tasks     = aws_security_group.match_tasks.id
  }
  security_group_id            = aws_security_group.match_ecs_endpoints.id
  description                  = "HTTPS from ${each.key}"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = each.value
}

# The ECS agent on each machine talks to ECS through these. Zone a only, like the ECR and logs
# endpoints (matches_bots.tf).
resource "aws_vpc_endpoint" "match_ecs" {
  for_each            = toset(["ecs", "ecs-agent", "ecs-telemetry"])
  vpc_id              = module.dummy_vpc.vpc_id
  service_name        = "com.amazonaws.${local.aws_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.match_bots["a"].id]
  security_group_ids  = [aws_security_group.match_ecs_endpoints.id]
  private_dns_enabled = true
  policy              = local.match_endpoint_policy
  tags                = { Name = "${local.matches_name}-${each.key}" }
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# What the ECS agent on a machine needs. Containers can't use it: the agent blocks the instance
# metadata service for awsvpc tasks (ECS_AWSVPC_BLOCK_IMDS).
resource "aws_iam_role" "match_bot_hosts" {
  name               = local.match_bot_hosts_name
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "match_bot_hosts" {
  role       = aws_iam_role.match_bot_hosts.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "match_bot_hosts" {
  name = local.match_bot_hosts_name
  role = aws_iam_role.match_bot_hosts.name
}

resource "aws_launch_template" "match_bot_hosts" {
  name                   = local.match_bot_hosts_name
  image_id               = local.match_bot_hosts_ami
  instance_type          = "c8a.medium"
  vpc_security_group_ids = [aws_security_group.match_bot_hosts.id]
  # one-bot.sh ends a machine with a shutdown.
  instance_initiated_shutdown_behavior = "terminate"

  iam_instance_profile {
    arn = aws_iam_instance_profile.match_bot_hosts.arn
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 30
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  user_data = base64encode(templatefile("${path.module}/bot_host/user-data.sh.tftpl", {
    cluster = aws_ecs_cluster.matches.name
    one_bot = file("${path.module}/bot_host/one-bot.sh")
  }))

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = local.match_bot_hosts_name }
  }
}

resource "aws_autoscaling_group" "match_bot_hosts" {
  name                = local.match_bot_hosts_name
  min_size            = 0
  max_size            = local.match_bot_hosts_max
  vpc_zone_identifier = [for subnet in aws_subnet.match_bots : subnet.id]
  # ECS decides which machines may go: never one that still runs a bot.
  protect_from_scale_in = true
  # Lets a PR remove the group while machines are still running.
  force_delete = true

  launch_template {
    id      = aws_launch_template.match_bot_hosts.id
    version = aws_launch_template.match_bot_hosts.latest_version
  }

  tag {
    key                 = "AmazonECSManaged"
    value               = true
    propagate_at_launch = true
  }

  lifecycle {
    # The capacity provider sets it.
    ignore_changes = [desired_capacity]
  }
}

resource "aws_ecs_capacity_provider" "match_bot_hosts" {
  name = local.match_bot_hosts_name

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.match_bot_hosts.arn
    managed_termination_protection = "ENABLED"

    managed_scaling {
      status                    = "ENABLED"
      target_capacity           = 100
      minimum_scaling_step_size = 1
      maximum_scaling_step_size = local.match_bot_hosts_max
      instance_warmup_period    = 90
    }
  }
}

# The same machines on Spot, from whichever of the three types and zones has the most capacity
# at the lowest price. ECS doesn't fall back to on-demand by itself: run_batch asks this group
# first and the on-demand group above for the rest. AWS can reclaim a Spot machine with two
# minutes' notice; the match task then voids that match (platform/prototype/task/match_task.py).
resource "aws_autoscaling_group" "match_bot_hosts_spot" {
  name                  = "${local.match_bot_hosts_name}-spot"
  min_size              = 0
  max_size              = local.match_bot_hosts_spot_max
  vpc_zone_identifier   = [for subnet in aws_subnet.match_bots : subnet.id]
  protect_from_scale_in = true
  force_delete          = true
  # No early replacements on a rebalance recommendation: a machine lives for one match anyway.
  capacity_rebalance = false

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0
      spot_allocation_strategy                 = "price-capacity-optimized"
    }

    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.match_bot_hosts.id
        version            = aws_launch_template.match_bot_hosts.latest_version
      }

      dynamic "override" {
        for_each = local.match_bot_hosts_spot_types
        content {
          instance_type = override.value
        }
      }
    }
  }

  tag {
    key                 = "AmazonECSManaged"
    value               = true
    propagate_at_launch = true
  }

  lifecycle {
    ignore_changes = [desired_capacity]
  }
}

resource "aws_ecs_capacity_provider" "match_bot_hosts_spot" {
  name = "${local.match_bot_hosts_name}-spot"

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.match_bot_hosts_spot.arn
    managed_termination_protection = "ENABLED"

    managed_scaling {
      status                    = "ENABLED"
      target_capacity           = 100
      minimum_scaling_step_size = 1
      maximum_scaling_step_size = local.match_bot_hosts_spot_max
      instance_warmup_period    = 90
    }
  }
}

# FARGATE stays listed so nothing changes for match tasks, which name their launch type.
resource "aws_ecs_cluster_capacity_providers" "matches" {
  cluster_name = aws_ecs_cluster.matches.name
  capacity_providers = [
    "FARGATE",
    aws_ecs_capacity_provider.match_bot_hosts.name,
    aws_ecs_capacity_provider.match_bot_hosts_spot.name,
  ]
}

# The bot on a machine of its own: the same container as the Fargate bot, sized to fill a
# machine's one vCPU (1,024 CPU units). A task bigger than the machine would wait forever.
resource "aws_ecs_task_definition" "match_bot_ec2" {
  family                   = "${local.match_bots_name}-ec2"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "1536"
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
