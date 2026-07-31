variable "project_id" {
  type        = string
  default     = "tidy-outlet-412020"
  description = "Google Cloud project that owns the OGRRE infrastructure."
}

variable "region" {
  type        = string
  default     = "us-central1"
  description = "Default Google Cloud region for regional resources."
}

variable "dns_managed_zone" {
  type        = string
  default     = "uow-carbon-org"
  description = "Cloud DNS managed zone used for OGRRE public DNS records."
}

variable "backend_dns_domain" {
  type        = string
  default     = "uow-carbon.org"
  description = "Base DNS domain for backend hostnames, without a trailing dot."
}

variable "legacy_backend_vms" {
  type = map(object({
    enable_startup_script  = bool
    zone                   = string
    machine_type           = string
    boot_image             = string
    boot_disk_size         = number
    boot_resource_policies = list(string)
    boot_disk_device_name  = string
  }))

  default = {
    isgs = {
      enable_startup_script  = false
      zone                   = "us-central1-a"
      machine_type           = "e2-standard-2"
      boot_image             = "https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-12-bookworm-v20240515"
      boot_disk_size         = 20
      boot_resource_policies = []
      boot_disk_device_name  = "isgs-uow-server"
    }

    osage = {
      enable_startup_script = false
      zone                  = "us-central1-f"
      machine_type          = "e2-standard-2"
      boot_image            = "https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-12-bookworm-v20250910"
      boot_disk_size        = 20
      boot_resource_policies = [
        "https://www.googleapis.com/compute/v1/projects/tidy-outlet-412020/regions/us-central1/resourcePolicies/default-schedule-1"
      ]
      boot_disk_device_name = "osage-uow-server"
    }

    ca = {
      enable_startup_script  = false
      zone                   = "us-central1-f"
      machine_type           = "e2-medium"
      boot_image             = "https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-12-bookworm-v20241009"
      boot_disk_size         = 10
      boot_resource_policies = []
      boot_disk_device_name  = "ca-uow-server"
    }

    newts = {
      enable_startup_script = false
      zone                  = "us-central1-b"
      machine_type          = "e2-standard-2"
      boot_image            = "https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-12-bookworm-v20260513"
      boot_disk_size        = 20
      boot_resource_policies = [
        "https://www.googleapis.com/compute/v1/projects/tidy-outlet-412020/regions/us-central1/resourcePolicies/default-schedule-1"
      ]
      boot_disk_device_name = "newts-ogrre-server"
    }

    staging = {
      enable_startup_script  = false
      zone                   = "us-central1-a"
      machine_type           = "e2-custom-medium-6400"
      boot_image             = "https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-11-bullseye-v20240110"
      boot_disk_size         = 20
      boot_resource_policies = []
      boot_disk_device_name  = "oprhaned-wells-ui-server-v0"
    }

    rrc = {
      enable_startup_script = false
      zone                  = "us-central1-b"
      machine_type          = "e2-standard-2"
      boot_image            = "https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-12-bookworm-v20260513"
      boot_disk_size        = 20
      boot_resource_policies = [
        "https://www.googleapis.com/compute/v1/projects/tidy-outlet-412020/regions/us-central1/resourcePolicies/default-schedule-1"
      ]
      boot_disk_device_name = "rrc-ogrre-server"
    }
  }

  description = "Legacy Compute Engine VM backends. Add entries here only when intentionally managing a VM."
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

variable "gke_backends" {
  type = map(object({
    namespace                 = optional(string)
    hostname                  = optional(string)
    test_hostname             = optional(string)
    static_ip_name            = optional(string)
    replicas                  = optional(number)
    cpu_request               = optional(string)
    memory_request            = optional(string)
    cpu_limit                 = optional(string)
    memory_limit              = optional(string)
    persistent_disk_size      = optional(string)
    create_primary_dns_record = optional(bool)
    create_test_dns_record    = optional(bool)
    dns_ttl                   = optional(number)
  }))

  default = {
    staging = {}
    osage   = {}
    isgs    = {}
    newts   = {}
    ca      = {}
    rrc     = {}
  }

  description = "Default GKE backend definitions. Keys are collaborator names; omitted attributes use OGRRE naming defaults."
}

variable "gke_backend_overrides" {
  type = map(object({
    namespace                 = optional(string)
    hostname                  = optional(string)
    test_hostname             = optional(string)
    static_ip_name            = optional(string)
    replicas                  = optional(number)
    cpu_request               = optional(string)
    memory_request            = optional(string)
    cpu_limit                 = optional(string)
    memory_limit              = optional(string)
    persistent_disk_size      = optional(string)
    create_primary_dns_record = optional(bool)
    create_test_dns_record    = optional(bool)
    dns_ttl                   = optional(number)
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
