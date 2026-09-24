terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
  # Add a backend block here when you have a shared state bucket, e.g.:
  # backend "s3" {
  #   bucket = "your-tfstate-bucket"
  #   key    = "eval-harvest/terraform.tfstate"
  #   region = "us-east-1"
  # }
}

provider "aws" {
  region = var.aws_region
}
