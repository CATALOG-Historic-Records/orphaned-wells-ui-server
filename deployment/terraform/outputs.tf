output "server_ips" {
  description = "GKE backend external IPs."
  value = var.enable_gke ? {
    for name, address in google_compute_global_address.gke_backend :
    name => address.address
  } : {}
}

output "dns_names" {
  description = "Primary backend DNS records."
  value = var.enable_gke ? {
    for name, dns in google_dns_record_set.gke_backend_primary :
    name => dns.name
  } : {}
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

output "gke_backend_upload_buckets" {
  value = var.enable_gke ? {
    for name, backend in local.gke_backends :
    name => backend.upload_bucket_name
  } : {}
}

output "gke_primary_dns_names" {
  value = var.enable_gke ? {
    for name, dns in google_dns_record_set.gke_backend_primary :
    name => dns.name
  } : {}
}

output "gke_test_dns_names" {
  value = var.enable_gke && var.create_gke_test_dns_records ? {
    for name, backend in local.gke_test_dns_backends :
    name => "${backend.test_hostname}."
  } : {}
}

output "kubernetes_deploy_targets" {
  value = var.enable_gke ? {
    for name, backend in local.kubernetes_workload_backends :
    name => {
      cluster_name                              = google_container_cluster.backend[0].name
      cluster_location                          = google_container_cluster.backend[0].location
      namespace                                 = backend.namespace
      host                                      = backend.hostname
      test_host                                 = backend.test_hostname
      static_ip_name                            = google_compute_global_address.gke_backend[name].name
      static_ip_address                         = google_compute_global_address.gke_backend[name].address
      storage_bucket_name                       = backend.upload_bucket_name
      replicas                                  = backend.replicas
      cpu_request                               = backend.cpu_request
      memory_request                            = backend.memory_request
      cpu_limit                                 = backend.cpu_limit
      memory_limit                              = backend.memory_limit
      persistent_disk_size                      = backend.persistent_disk_size
      api_uvicorn_workers                       = backend.api_uvicorn_workers
      processing_job_cpu_request                = backend.processing_job_cpu_request
      processing_job_memory_request             = backend.processing_job_memory_request
      processing_job_cpu_limit                  = backend.processing_job_cpu_limit
      processing_job_memory_limit               = backend.processing_job_memory_limit
      processing_job_ephemeral_storage          = backend.processing_job_ephemeral_storage
      processing_job_active_deadline_seconds    = backend.processing_job_active_deadline_seconds
      processing_job_ttl_seconds_after_finished = backend.processing_job_ttl_seconds_after_finished
      processing_job_max_active                 = backend.processing_job_max_active
    }
  } : {}
}
