# Keep externally pulled model workers separate from the private application and
# Hermes images. Runpod must never need access to those repositories.
resource "aws_ecr_repository" "embedding" {
  name                 = "${var.name}/embedding"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "embedding" {
  repository = aws_ecr_repository.embedding.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged embedding build intermediates after 14 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 14
      }
      action = { type = "expire" }
    }]
  })
}

# Runpod's documented fixed deployment roles. Scope their pull permission to
# this model-worker repository; application images remain inaccessible.
# https://docs.runpod.io/tutorials/pods/use-private-ecr-images
resource "aws_ecr_repository_policy" "embedding_runpod" {
  repository = aws_ecr_repository.embedding.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowRunpodEmbeddingPull"
      Effect    = "Allow"
      Principal = "*"
      Action = [
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
      ]
      Condition = {
        StringEquals = {
          "aws:PrincipalArn" = [
            "arn:aws:iam::550005742258:role/prod-us-east-1-deployment-role",
            "arn:aws:iam::550005742258:role/prod-us-west-2-deployment-role",
          ]
        }
      }
    }]
  })
}

output "embedding_repository" {
  value = {
    arn = aws_ecr_repository.embedding.arn
    url = aws_ecr_repository.embedding.repository_url
  }
}
