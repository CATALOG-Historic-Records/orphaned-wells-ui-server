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

variable "dns_rrdatas_override" {
  type        = list(string)
  default     = null
  description = "Optional DNS A-record values. When unset, DNS points at this VM's static IP."
}
