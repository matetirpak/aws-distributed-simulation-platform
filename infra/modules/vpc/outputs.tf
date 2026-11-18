output "vpc_id" {
  description = "VPC ID."
  value       = aws_vpc.this.id
}

output "public_subnet_id" {
  description = "ID of the public subnet."
  value       = aws_subnet.public.id
}

output "public_route_table_id" {
  description = "The route table with the route to the internet gateway."
  value       = aws_route_table.public.id
}
