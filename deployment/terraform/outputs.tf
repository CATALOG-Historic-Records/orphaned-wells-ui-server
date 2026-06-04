# module "backend_vms" {
#   for_each = local.collaborators
# }

output "server_ips" {
  value = {
    for name, vm in module.backend_vms :
    name => vm.ip
  }
}

output "isgs_ip" {
  value = module.backend_vms["isgs"].ip
}

output "dns_names" {
  value = {
    for name, vm in module.backend_vms :
    name => vm.dns_name
  }
}