terraform {
  required_version = "~> 1.13.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0, < 7.0.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.30.0, < 3.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
}

resource "google_compute_firewall" "backend_http_https" {
  name    = "backend-http-https"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }

  # Open to everyone
  source_ranges = ["0.0.0.0/0"]

  target_tags = ["backend"]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_firewall" "backend_ssh" {
  name    = "backend-ssh"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  # Restricted LBL VPN SSH source range
  source_ranges = [
    "128.3.0.0/16",
    "131.243.0.0/16",
    "35.235.240.0/20",
  ]

  target_tags = ["backend"]

  lifecycle {
    prevent_destroy = true
  }
}
