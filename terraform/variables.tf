variable "plugin_sha256" {
  description = "SHA-256 of the oauthapp plugin binary baked into the OpenBao image."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.plugin_sha256))
    error_message = "plugin_sha256 must be a 64-character lowercase hex SHA-256."
  }
}

variable "app_namespace" {
  description = "Namespace the aggregator runs in."
  type        = string
  default     = "aggregator"
}

variable "app_service_account" {
  description = "Service account the aggregator logs in with."
  type        = string
  default     = "aggregator"
}
