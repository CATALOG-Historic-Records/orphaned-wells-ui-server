resource "google_compute_address" "isgs-static-ip-address" {
  name = "isgs-static-ip-address"
  project = "tidy-outlet-412020"
  region = "us-central1"
}


resource "google_compute_network" "vpc_network" {
  name                    = "my-custom-mode-network"
  auto_create_subnetworks = false
  mtu                     = 1460
  project = "tidy-outlet-412020"
}

resource "google_compute_subnetwork" "default" {
  name          = "my-custom-subnet"
  ip_cidr_range = "10.0.1.0/24"
  region        = "us-central1"
  network       = google_compute_network.vpc_network.id
  project = "tidy-outlet-412020"
}

resource "google_compute_instance" "isgs-uow-server" {
  name         = "isgs-uow-server"
  machine_type = "e2-medium"
  zone         = "us-central1-a"
  tags         = ["http-server", "https-server", "tcp8001", "ssh"]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-11"
      size  = 10
      type  = "pd-balanced"
    }

    mode   = "READ_WRITE"
  }

  network_interface {
    subnetwork = google_compute_subnetwork.default.id

    access_config {
      # Include this section to give the VM an external IP address
      nat_ip = "${google_compute_address.isgs-static-ip-address.address}"
    }
  }

  project = "tidy-outlet-412020"

  reservation_affinity {
    type = "ANY_RESERVATION"
  }

  scheduling {
    automatic_restart   = true
    on_host_maintenance = "MIGRATE"
    provisioning_model  = "STANDARD"
  }

  service_account {
    email  = "1095146523031-compute@developer.gserviceaccount.com"
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  shielded_instance_config {
    enable_integrity_monitoring = true
    enable_vtpm                 = true
  }
  
}