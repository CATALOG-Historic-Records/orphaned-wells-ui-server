variable "project_id" {
  type        = string
  description = "Google Cloud project that owns the OGRRE infrastructure."
}

variable "region" {
  type        = string
  default     = "us-central1"
  description = "Default Google Cloud region for regional resources."
}

variable "dns_managed_zone" {
  type        = string
  description = "Cloud DNS managed zone used for OGRRE public DNS records."
}

variable "backend_dns_domain" {
  type        = string
  description = "Base DNS domain for backend hostnames, without a trailing dot."
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

variable "upload_bucket_cors_origins" {
  type        = map(list(string))
  default     = {}
  description = "Optional complete CORS origin list per upload bucket. Defaults to frontend custom domains, plus http://localhost:3000 and http://localhost:3001 for the staging bucket. Overrides replace the complete list."

  validation {
    condition = alltrue(flatten([
      for origins in values(var.upload_bucket_cors_origins) : [
        for origin in origins : can(regex("^https?://[^/]+$", origin))
      ]
    ]))
    error_message = "CORS origins must be explicit HTTP(S) origins without trailing slashes."
  }
}

variable "gke_backends" {
  type = map(object({
    namespace                                 = optional(string)
    hostname                                  = optional(string)
    test_hostname                             = optional(string)
    static_ip_name                            = optional(string)
    upload_bucket_name                        = optional(string)
    upload_bucket_location                    = optional(string)
    replicas                                  = optional(number)
    cpu_request                               = optional(string)
    memory_request                            = optional(string)
    cpu_limit                                 = optional(string)
    memory_limit                              = optional(string)
    persistent_disk_size                      = optional(string)
    api_uvicorn_workers                       = optional(number)
    processing_job_cpu_request                = optional(string)
    processing_job_memory_request             = optional(string)
    processing_job_cpu_limit                  = optional(string)
    processing_job_memory_limit               = optional(string)
    processing_job_ephemeral_storage          = optional(string)
    processing_job_active_deadline_seconds    = optional(number)
    processing_job_ttl_seconds_after_finished = optional(number)
    processing_job_max_active                 = optional(number)
    enable_kubernetes_workloads               = optional(bool)
    create_primary_dns_record                 = optional(bool)
    create_test_dns_record                    = optional(bool)
    dns_ttl                                   = optional(number)
  }))

  default = {}

  description = "Default GKE backend definitions. Keys are collaborator names; omitted attributes use OGRRE naming defaults. Set enable_kubernetes_workloads=false to retain cloud configuration without making the backend deployable to Kubernetes."
}

variable "gke_backend_overrides" {
  type = map(object({
    namespace                                 = optional(string)
    hostname                                  = optional(string)
    test_hostname                             = optional(string)
    static_ip_name                            = optional(string)
    upload_bucket_name                        = optional(string)
    upload_bucket_location                    = optional(string)
    replicas                                  = optional(number)
    cpu_request                               = optional(string)
    memory_request                            = optional(string)
    cpu_limit                                 = optional(string)
    memory_limit                              = optional(string)
    persistent_disk_size                      = optional(string)
    api_uvicorn_workers                       = optional(number)
    processing_job_cpu_request                = optional(string)
    processing_job_memory_request             = optional(string)
    processing_job_cpu_limit                  = optional(string)
    processing_job_memory_limit               = optional(string)
    processing_job_ephemeral_storage          = optional(string)
    processing_job_active_deadline_seconds    = optional(number)
    processing_job_ttl_seconds_after_finished = optional(number)
    processing_job_max_active                 = optional(number)
    enable_kubernetes_workloads               = optional(bool)
    create_primary_dns_record                 = optional(bool)
    create_test_dns_record                    = optional(bool)
    dns_ttl                                   = optional(number)
  }))

  default     = {}
  description = "Additional GKE backends or per-backend overrides merged over gke_backends. Use this from terraform.tfvars for local additions."
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

variable "upload_bucket_location" {
  type        = string
  default     = "US"
  description = "Default Cloud Storage location for newly created collaborator upload buckets."
}
