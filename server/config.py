"""OCTO-CRM-APM — Configuration.

ATP-only runtime configuration for OKE and OCI deployments.
"""

import os


class Config:
    app_name = os.getenv("APP_NAME", os.getenv("OBSERVABILITY_APP_NAME", "enterprise-crm-portal"))
    app_runtime = os.getenv("APP_RUNTIME", "oke")
    otel_service_name = os.getenv(
        "OTEL_SERVICE_NAME",
        os.getenv("OBSERVABILITY_SERVICE_NAME", "enterprise-crm-portal-oke"),
    )
    oci_auth_mode = os.getenv("OCI_AUTH_MODE", "auto")
    port = int(os.getenv("PORT", "8080"))
    environment = os.getenv("ENVIRONMENT", "production")
    auth_token_secret = os.getenv("AUTH_TOKEN_SECRET", "")

    # ── Database (Oracle ATP only) ──
    oracle_dsn = os.getenv("ORACLE_DSN", "")  # e.g. "(description=...)" or TNS alias
    oracle_user = os.getenv("ORACLE_USER", "ADMIN")
    oracle_password = os.getenv("ORACLE_PASSWORD", "")
    oracle_wallet_dir = os.getenv("ORACLE_WALLET_DIR", "")
    oracle_wallet_password = os.getenv("ORACLE_WALLET_PASSWORD", "")

    # ── Cross-service integration ──
    enterprise_crm_url = os.getenv("ENTERPRISE_CRM_URL", "")

    # ── OCI APM ──
    oci_apm_endpoint = os.getenv("OCI_APM_ENDPOINT", "")
    oci_apm_private_datakey = os.getenv("OCI_APM_PRIVATE_DATAKEY", "")
    oci_apm_public_datakey = os.getenv("OCI_APM_PUBLIC_DATAKEY", "")
    oci_apm_rum_endpoint = os.getenv("OCI_APM_RUM_ENDPOINT", "")
    oci_apm_web_application = os.getenv("OCI_APM_WEB_APPLICATION", "octo-drone-shop")

    # ── OCI Logging SDK ──
    oci_log_id = os.getenv("OCI_LOG_ID", "")
    oci_log_group_id = os.getenv("OCI_LOG_GROUP_ID", "")

    # ── OCI Generative AI ──
    oci_compartment_id = os.getenv("OCI_COMPARTMENT_ID", "")
    oci_genai_endpoint = os.getenv("OCI_GENAI_ENDPOINT", "")
    oci_genai_model_id = os.getenv("OCI_GENAI_MODEL_ID", "")

    # ── Splunk HEC ──
    splunk_hec_url = os.getenv("SPLUNK_HEC_URL", "")
    splunk_hec_token = os.getenv("SPLUNK_HEC_TOKEN", "")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def crm_hostname(self) -> str:
        if not self.enterprise_crm_url:
            return ""
        return self.enterprise_crm_url.split("://", 1)[-1].split("/", 1)[0]

    @property
    def apm_configured(self) -> bool:
        return bool(self.oci_apm_endpoint and self.oci_apm_private_datakey)

    @property
    def rum_configured(self) -> bool:
        return bool(self.oci_apm_rum_endpoint and self.oci_apm_public_datakey)

    @property
    def logging_configured(self) -> bool:
        return bool(self.oci_log_id)

    @property
    def database_url(self) -> str:
        """Return async Oracle ATP database URL for SQLAlchemy."""
        return f"oracle+oracledb_async://{self.oracle_user}:{self.oracle_password}@"

    def masked_database_url(self) -> str:
        return f"oracle+oracledb_async://{self.oracle_user}:***@"

    def safe_runtime_summary(self) -> dict:
        return {
            "app_name": self.app_name,
            "environment": self.environment,
            "app_runtime": self.app_runtime,
            "database_backend": "oracle_atp",
            "database_configured": bool(self.oracle_dsn and self.oracle_password),
            "apm_configured": self.apm_configured,
            "rum_configured": self.rum_configured,
            "logging_configured": self.logging_configured,
            "splunk_configured": bool(self.splunk_hec_url and self.splunk_hec_token),
            "genai_configured": bool(self.oci_compartment_id and self.oci_genai_endpoint and self.oci_genai_model_id),
            "crm_configured": bool(self.enterprise_crm_url),
            "crm_host": self.crm_hostname or None,
        }

    def validate(self) -> None:
        missing = []
        if not self.oracle_dsn:
            missing.append("ORACLE_DSN")
        if not self.oracle_password:
            missing.append("ORACLE_PASSWORD")
        if missing:
            values = ", ".join(missing)
            raise RuntimeError(f"ATP-only mode requires: {values}")


cfg = Config()
