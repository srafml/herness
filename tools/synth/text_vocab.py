"""Built-in vocabularies and template skeletons of `tools.synth.text` (U11-05, design §5.1.4).

Split out of `tools.synth.text` for the module line budget. Every string is invented; no
real data (TH11-01). Sizes: 60 symptoms, 25 root causes (each mapped to exactly one of
`ROOT_CAUSE_OPTIONS`), 40 actions, 30 families.
"""

from typing import Final


def _split(block: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in block.split(";") if item.strip())


ROOT_CAUSE_OPTIONS: Final = _split("""software_defect; config_change; capacity; infrastructure;
    dependency; data_issue; access_identity; user_error; unknown""")
CHANGE_MARKERS: Final = _split(
    "after the release; following change; tras el despliegue; después del cambio"
)
REPEAT_MARKERS: Final = ("again", "recurring", "otra vez", "recurrente")

SYMPTOMS: Final = _split("""
    slow page loads; login failures; timeouts on save; blank screens; error 500 responses;
    error 503 responses; stuck spinners; failed uploads; missing search results;
    duplicate notifications; failed payments; delayed emails; broken report exports;
    session drops; high response times; failed batch runs; queue delays; stale dashboards;
    wrong totals in reports; failed API calls; connection resets; SSL handshake errors;
    access denied messages; locked accounts; failed file transfers; slow queries;
    database deadlocks; out of memory errors; crashed worker processes; missing invoices;
    failed password resets; disk alerts; high CPU alerts; packet loss alerts;
    DNS lookup errors; failed health checks; stuck orders; unreadable PDFs;
    empty data feeds; failed sync jobs; mobile app crashes; printing failures;
    VPN disconnects; slow file shares; failed backups; certificate warnings;
    broken links; missing attachments; frozen screens; bad gateway errors;
    garbled characters; delayed shipments; failed webhooks; rejected transactions;
    slow checkout; lost shopping carts; unsent SMS messages; failed logins via SSO;
    corrupted downloads; missing audit entries
""")
ACTIONS: Final = _split("""
    restarted the service; increased the pool size; rolled back the deployment;
    renewed the certificate; unlocked the service account; cleared the cache;
    added disk space; scaled out the web tier; failed over to the standby node;
    replaced the faulty disk; rebooted the host; reran the batch job;
    corrected the configuration value; disabled the feature flag; fixed the load balancer rule;
    rotated the API credential; reloaded the reference data; removed duplicate records;
    patched the application; applied the vendor hotfix; purged the message queue;
    restarted the message broker; switched DNS provider; raised the thread limit;
    killed the runaway query; rebuilt the index; restored files from backup;
    reprocessed the data feed; updated the schema mapping; reset the user password;
    moved workloads to another host; replaced the network cable; tuned the garbage collector;
    throttled the batch window; opened a vendor ticket; added monitoring;
    reverted the script changes; resynced the replicas; extended the timeout;
    documented a workaround
""")
# ≈ 25 root causes, each mapped to exactly one of ROOT_CAUSE_OPTIONS.
CAUSES: Final = tuple(
    (phrase.strip(), option.strip())
    for phrase, option in (
        row.split("=")
        for row in _split("""
    a null pointer defect in the new build = software_defect;
    a memory leak in the application = software_defect;
    a race condition in the job scheduler = software_defect;
    a wrong setting pushed in the configuration = config_change;
    a misconfigured load balancer rule = config_change;
    an incorrect feature flag value = config_change;
    connection pool exhaustion under peak load = capacity; disk space running out = capacity;
    thread pool saturation = capacity; CPU saturation during batch jobs = capacity;
    a failed storage controller = infrastructure; a network switch failure = infrastructure;
    a hypervisor host crash = infrastructure; an upstream payment API outage = dependency;
    a slow third-party DNS provider = dependency; a message broker outage = dependency;
    corrupted records in the import file = data_issue;
    a duplicate key in the reference data = data_issue;
    a schema mismatch in the data feed = data_issue; an expired TLS certificate = access_identity;
    a locked service account = access_identity; an expired API credential = access_identity;
    an operator running the wrong script = user_error;
    a user deleting a shared folder = user_error; no cause identified = unknown
    """)
    )
)
# (name, English topic, Spanish topic, index into CAUSES)
FAMILY_ROWS: Final = (
    ("tpl_app_crash", "application crashes", "caídas de la aplicación", 0),
    ("tpl_mem_leak", "growing memory usage", "consumo creciente de memoria", 1),
    ("tpl_job_stuck", "stuck scheduled jobs", "trabajos programados bloqueados", 2),
    ("tpl_bad_config", "configuration errors", "errores de configuración", 3),
    ("tpl_lb_routing", "requests routed to the wrong pool", "peticiones mal enrutadas", 4),
    ("tpl_feature_flag", "unexpected feature behaviour", "funciones con comportamiento raro", 5),
    ("tpl_conn_pool", "connection pool exhaustion", "agotamiento del pool de conexiones", 6),
    ("tpl_disk_full", "disk full warnings", "avisos de disco lleno", 7),
    ("tpl_thread_pool", "thread pool saturation", "saturación de hilos", 8),
    ("tpl_cpu_high", "high CPU load", "carga alta de CPU", 9),
    ("tpl_storage", "storage latency spikes", "picos de latencia de almacenamiento", 10),
    ("tpl_network", "packet loss", "pérdida de paquetes", 11),
    ("tpl_vm_down", "virtual machines going down", "máquinas virtuales caídas", 12),
    ("tpl_payment_api", "payment API errors", "errores de la API de pagos", 13),
    ("tpl_dns", "name resolution failures", "fallos de resolución de nombres", 14),
    ("tpl_queue_backlog", "message queue backlog", "cola de mensajes acumulada", 15),
    ("tpl_bad_import", "failed data imports", "importaciones de datos fallidas", 16),
    ("tpl_dup_records", "duplicate record errors", "registros duplicados", 17),
    ("tpl_feed_schema", "data feed rejections", "rechazos del feed de datos", 18),
    ("tpl_cert_expiry", "certificate expiry errors", "certificado caducado", 19),
    ("tpl_account_lock", "service account lockouts", "bloqueo de cuentas de servicio", 20),
    ("tpl_token_expiry", "authentication failures", "fallos de autenticación", 21),
    ("tpl_wrong_script", "unexpected mass updates", "actualizaciones masivas inesperadas", 22),
    ("tpl_data_deleted", "missing shared files", "archivos compartidos perdidos", 23),
    ("tpl_intermittent", "intermittent errors", "errores intermitentes", 24),
    ("tpl_slow_pages", "slow application pages", "páginas lentas", 8),
    ("tpl_login_errors", "login errors", "errores de inicio de sesión", 20),
    ("tpl_gateway_timeout", "gateway timeouts", "tiempos de espera en la pasarela", 13),
    ("tpl_report_wrong", "wrong report figures", "cifras erróneas en informes", 17),
    ("tpl_batch_fail", "batch job failures", "fallos de procesos batch", 9),
)
PATTERNS_EN: Final = (
    "{Symptom} on {component}; {topic} observed on {host}.",
    "Users report {symptom} in {component}. Monitoring shows {topic} on {host}.",
    "{component} is showing {symptom}. Investigation points to {topic}.",
    "Alert from {host}: {topic} affecting {component}, {count} users report {symptom}.",
    "{count} users cannot work: {symptom} in {component}, linked to {topic}.",
    "Service desk ticket: {symptom} for {component} with {topic} on {host}.",
)
CHANGE_EN: Final = (
    "After the release to {component}, {symptom} started; {topic} seen on {host}.",
    "Following change to {component}, users report {symptom} and {topic}.",
)
PATTERNS_ES: Final = (
    "Los usuarios reportan problemas en {component}: {topic} en {host}.",
    "{component} presenta fallos; se detecta {topic} ({count} usuarios afectados).",
    "Incidencia en {component} por {topic} en el servidor {host}.",
)
CHANGE_ES: Final = (
    "Tras el despliegue en {component}, se detecta {topic} en {host}.",
    "Después del cambio en {component}, aparece {topic} ({count} usuarios afectados).",
)
REPEAT: Final = {
    "en": ("This is happening again.", "Recurring issue."),
    "es": ("Ocurre otra vez.", "Problema recurrente."),
}
IMPACT: Final = {
    "en": _split("""No user impact reported.; Minor impact on a few users.;
        Service degraded for many users.; Full outage: service unavailable."""),
    "es": _split("""Sin impacto para usuarios.; Impacto menor en pocos usuarios.;
        Servicio degradado para muchos usuarios.; Caída total: servicio no disponible."""),
}
OPTION_ES: Final = dict(
    zip(
        ROOT_CAUSE_OPTIONS,
        _split("""defecto de software; cambio de configuración; falta de capacidad;
        fallo de infraestructura; fallo de un proveedor; datos erróneos;
        credenciales o certificados; error de usuario; causa desconocida"""),
        strict=True,
    )
)
HOSTS: Final = ("app", "db", "web", "api", "cache", "mq", "batch", "gw")
JIRA_SUMMARY: Final = {
    "initiative": "Modernize {component} to remove {topic}",
    "epic": "Reduce {topic} in {component}",
    "feature": "Add monitoring for {topic} in {component}",
    "story": "As an operator I want alerts for {topic} on {component}",
    "bug": "Fix {topic} in {component}",
    "task": "{Action} for {component}",
}
CHANGE_VERBS: Final = ("Upgrade", "Patch", "Reconfigure", "Scale out", "Migrate", "Restart")

__all__ = [
    "ACTIONS",
    "CAUSES",
    "CHANGE_EN",
    "CHANGE_ES",
    "CHANGE_MARKERS",
    "CHANGE_VERBS",
    "FAMILY_ROWS",
    "HOSTS",
    "IMPACT",
    "JIRA_SUMMARY",
    "OPTION_ES",
    "PATTERNS_EN",
    "PATTERNS_ES",
    "REPEAT",
    "REPEAT_MARKERS",
    "ROOT_CAUSE_OPTIONS",
    "SYMPTOMS",
]
