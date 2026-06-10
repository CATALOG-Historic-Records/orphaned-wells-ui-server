resource "google_compute_address" "ip" {
  name = "${var.collaborator}-static-ip-address"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_instance" "vm" {
  name = "${var.collaborator}-uow-server"
  zone = var.zone

  tags = ["backend", "http-server", "https-server"]

  machine_type = var.machine_type

  boot_disk {
    auto_delete = true
    device_name = var.boot_disk_device_name

    initialize_params {
      image             = var.boot_image
      size              = var.boot_disk_size
      type              = "pd-balanced"
      resource_policies = var.boot_resource_policies
    }
  }


  network_interface {
    network = "default"

    access_config {
      nat_ip = google_compute_address.ip.address
    }
  }

  service_account {
    scopes = ["cloud-platform"]
  }

  metadata = {
    enable-osconfig = "TRUE"
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes = [
      metadata["ssh-keys"],
    ]
  }

  shielded_instance_config {
    enable_integrity_monitoring = true
    enable_secure_boot          = false
    enable_vtpm                 = true

    # key_revocation_action_type = "NONE"
  }
  key_revocation_action_type = "NONE"


  metadata_startup_script = var.enable_startup_script ? file("${path.module}/startup.sh") : null
}

resource "google_dns_record_set" "dns" {
  name = "${var.collaborator}-server.uow-carbon.org."
  type = "A"
  ttl  = 300

  managed_zone = "uow-carbon-org"

  rrdatas = var.dns_rrdatas_override != null ? var.dns_rrdatas_override : [
    google_compute_address.ip.address
  ]

  lifecycle {
    prevent_destroy = true
  }
}
