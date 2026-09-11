data "google_client_config" "current" {}

# Terraform owns long-lived Kubernetes identity and authorization objects.
# GitHub Actions deploys the application workload but cannot grant Kubernetes
# permissions to itself or to the runtime Pods.
provider "kubernetes" {
  host  = try("https://${google_container_cluster.backend[0].endpoint}", null)
  token = try(data.google_client_config.current.access_token, null)
  cluster_ca_certificate = try(
    base64decode(google_container_cluster.backend[0].master_auth[0].cluster_ca_certificate),
    null,
  )
}

resource "kubernetes_namespace_v1" "backend" {
  for_each = var.enable_gke ? local.kubernetes_workload_backends : {}

  metadata {
    name = each.value.namespace
    labels = {
      "app.kubernetes.io/name"    = "orphaned-wells-ui-server"
      "app.kubernetes.io/part-of" = "uow-backends"
      "uow.lbl.gov/environment"   = each.key
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_service_account_v1" "backend_api" {
  for_each = var.enable_gke ? local.kubernetes_workload_backends : {}

  metadata {
    name      = "backend-api"
    namespace = kubernetes_namespace_v1.backend[each.key].metadata[0].name
    labels = {
      "app.kubernetes.io/name"      = "orphaned-wells-ui-server"
      "app.kubernetes.io/component" = "api"
      "uow.lbl.gov/environment"     = each.key
    }
  }
}

resource "kubernetes_service_account_v1" "processing_worker" {
  for_each = var.enable_gke ? local.kubernetes_workload_backends : {}

  metadata {
    name      = "processing-worker"
    namespace = kubernetes_namespace_v1.backend[each.key].metadata[0].name
    labels = {
      "app.kubernetes.io/name"      = "orphaned-wells-ui-server"
      "app.kubernetes.io/component" = "processor"
      "uow.lbl.gov/environment"     = each.key
    }
  }
}

# The API creates a short-lived worker Job and reads its state. The worker does
# not need Kubernetes API permissions: it uses MongoDB for job state instead.
resource "kubernetes_role_v1" "processing_job_dispatcher" {
  for_each = var.enable_gke ? local.kubernetes_workload_backends : {}

  metadata {
    name      = "processing-job-dispatcher"
    namespace = kubernetes_namespace_v1.backend[each.key].metadata[0].name
  }

  rule {
    api_groups = ["batch"]
    resources  = ["jobs"]
    verbs      = ["create", "get", "list", "watch"]
  }

  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list", "watch"]
  }
}

resource "kubernetes_role_binding_v1" "processing_job_dispatcher" {
  for_each = var.enable_gke ? local.kubernetes_workload_backends : {}

  metadata {
    name      = "processing-job-dispatcher"
    namespace = kubernetes_namespace_v1.backend[each.key].metadata[0].name
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.processing_job_dispatcher[each.key].metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.backend_api[each.key].metadata[0].name
    namespace = kubernetes_namespace_v1.backend[each.key].metadata[0].name
  }
}
