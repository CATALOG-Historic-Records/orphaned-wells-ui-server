variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "machine_type" {
  type    = string
  default = "e2-medium"
}

variable "enable_gke" {
  type        = bool
  default     = true
  description = "Create the GKE cluster, Kubernetes ingress IPs, and GKE test DNS records."
}

variable "manage_project_services" {
  type        = bool
  default     = true
  description = "When enable_gke is true, enable the Google APIs required by this Terraform stack."
}

variable "gke_cluster_name" {
  type        = string
  default     = "uow-backend-gke"
  description = "Name of the shared GKE cluster for backend deployments."
}

variable "gke_location" {
  type        = string
  default     = "us-central1"
  description = "Regional or zonal location for the GKE cluster."
}

variable "gke_release_channel" {
  type        = string
  default     = "REGULAR"
  description = "GKE release channel for the cluster."

  validation {
    condition     = contains(["RAPID", "REGULAR", "STABLE"], var.gke_release_channel)
    error_message = "gke_release_channel must be RAPID, REGULAR, or STABLE."
  }
}

variable "gke_deletion_protection" {
  type        = bool
  default     = true
  description = "Enable GKE deletion protection on the cluster."
}

variable "gke_network" {
  type        = string
  default     = "default"
  description = "VPC network used by the GKE cluster."
}

variable "gke_subnetwork" {
  type        = string
  default     = "default"
  description = "VPC subnetwork used by the GKE cluster."
}

variable "gke_backend_hostnames" {
  type        = map(string)
  default     = {}
  description = "Optional canonical hostnames used by Kubernetes Ingress per backend, without a trailing dot. Defaults to <env>-k8s-server.uow-carbon.org."
}

variable "primary_dns_to_gke_backends" {
  type        = list(string)
  default     = []
  description = "Backend names whose existing <env>-server.uow-carbon.org DNS record should point to the GKE load balancer IP instead of the VM IP."
}

variable "create_gke_test_dns_records" {
  type        = bool
  default     = true
  description = "Create <env>-k8s-server.uow-carbon.org DNS records for staging/cutover testing."
}

variable "gke_dns_ttl" {
  type        = number
  default     = 300
  description = "TTL for GKE test DNS A records."
}
