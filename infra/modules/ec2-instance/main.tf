# A single throwaway instance, reachable only through SSM Session Manager: no key
# pair, no inbound rules, no public SSH. The agent dials out, so the box needs a
# route to the internet (an IGW is enough) but nothing needs a route in.

# Newest Amazon Linux 2023 image published by Amazon itself. owners is what keeps
# a lookalike name from matching someone else's AMI.
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-6.1-x86_64"]
  }
}

resource "aws_security_group" "this" {
  name        = var.name
  description = "No inbound. Outbound only, for the SSM agent."
  vpc_id      = var.vpc_id

  egress {
    description = "All outbound."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge({ Name = var.name }, var.tags)
}

# Who may wear the role: the EC2 service, on the instance's behalf.
data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = "${var.name}-instance"
  description        = "Lets ${var.name} register with SSM and open sessions."
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

# What the role may do. AWS-managed: registration, the message channels Session
# Manager rides on, and read-only access to its own SSM documents.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.this.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# EC2 cannot hold a role directly, only a profile that wraps one.
resource "aws_iam_instance_profile" "this" {
  name = "${var.name}-instance"
  role = aws_iam_role.this.name
}

resource "aws_instance" "this" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [aws_security_group.this.id]
  iam_instance_profile   = aws_iam_instance_profile.this.name

  # IMDSv2 only: a session token is required, so the instance's credentials
  # cannot be read through a server-side request forgery bug.
  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    encrypted = true
  }

  tags = merge({ Name = var.name }, var.tags)

  lifecycle {
    # most_recent = true would otherwise replace the instance every time Amazon
    # publishes a new AL2023 image. Taking a newer AMI is a deliberate act:
    # comment this out, apply, put it back.
    ignore_changes = [ami]
  }
}
