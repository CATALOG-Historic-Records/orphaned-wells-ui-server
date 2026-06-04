output "ip" {
  value = google_compute_address.ip.address
}

output "vm_name" {
  value = google_compute_instance.vm.name
}

output "dns_name" {
  value = google_dns_record_set.dns.name
}