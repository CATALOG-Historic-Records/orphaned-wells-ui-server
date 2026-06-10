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

output "gke_cluster_name" {
  value = var.enable_gke ? google_container_cluster.backend[0].name : null
}

output "gke_cluster_location" {
  value = var.enable_gke ? google_container_cluster.backend[0].location : null
}

output "gke_backend_static_ips" {
  value = var.enable_gke ? {
    for name, address in google_compute_global_address.gke_backend :
    name => address.address
  } : {}
}

output "gke_test_dns_names" {
  value = var.enable_gke ? {
    for name, backend in local.gke_backends :
    name => "${backend.test_hostname}."
  } : {}
}

output "kubernetes_deploy_targets" {
  value = var.enable_gke ? {
    for name, backend in local.gke_backends :
    name => {
      cluster_name         = google_container_cluster.backend[0].name
      cluster_location     = google_container_cluster.backend[0].location
      namespace            = backend.namespace
      host                 = backend.hostname
      test_host            = backend.test_hostname
      static_ip_name       = google_compute_global_address.gke_backend[name].name
      static_ip_address    = google_compute_global_address.gke_backend[name].address
      replicas             = backend.replicas
      cpu_request          = backend.cpu_request
      memory_request       = backend.memory_request
      cpu_limit            = backend.cpu_limit
      memory_limit         = backend.memory_limit
      persistent_disk_size = backend.persistent_disk_size
    }
  } : {}
}
