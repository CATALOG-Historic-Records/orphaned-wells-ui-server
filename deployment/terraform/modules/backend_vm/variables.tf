variable "collaborator" {
  type = string
}

variable "zone" {
  type = string
}

variable "enable_startup_script" {
  type    = bool
  default = false
}
variable "machine_type" {
  type = string
}

variable "boot_image" {
  type = string
}

variable "boot_disk_size" {
  type = number
}

variable "boot_disk_device_name" {
  type = string
}

variable "boot_resource_policies" {
  type    = list(string)
  default = []
}

variable "create_dns_record" {
  type        = bool
  default     = true
  description = "Create the legacy primary backend DNS record pointing at the VM IP."
}

variable "dns_managed_zone" {
  type        = string
  default     = "uow-carbon-org"
  description = "Cloud DNS managed zone used for the legacy backend DNS record."
}

variable "backend_dns_domain" {
  type        = string
  default     = "uow-carbon.org"
  description = "Base DNS domain for legacy backend hostnames, without a trailing dot."
}
