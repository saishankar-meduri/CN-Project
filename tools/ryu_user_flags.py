"""Register controller options before Ryu parses command-line arguments."""
from ryu import cfg

cfg.CONF.register_cli_opt(
    cfg.StrOpt(
        "mode",
        default="baseline",
        help="Controller operation mode: 'baseline' (detection only) or 'dynamic' (active mitigation)",
    )
)
