moved {
  from = module.backend_vms["staging"].google_dns_record_set.dns
  to   = google_dns_record_set.gke_backend_primary["staging"]
}

moved {
  from = module.backend_vms["osage"].google_dns_record_set.dns
  to   = google_dns_record_set.gke_backend_primary["osage"]
}

moved {
  from = module.backend_vms["isgs"].google_dns_record_set.dns
  to   = google_dns_record_set.gke_backend_primary["isgs"]
}

moved {
  from = module.backend_vms["newts"].google_dns_record_set.dns
  to   = google_dns_record_set.gke_backend_primary["newts"]
}

moved {
  from = module.backend_vms["ca"].google_dns_record_set.dns
  to   = google_dns_record_set.gke_backend_primary["ca"]
}

moved {
  from = module.backend_vms["rrc"].google_dns_record_set.dns
  to   = google_dns_record_set.gke_backend_primary["rrc"]
}
