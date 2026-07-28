"""
Evaluation Reporting
====================

Generates CSV / Excel / JSON / PDF reports from any evaluation artifact.
"""
from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from src.core.helpers import ensure_dir, utc_now_iso
from src.core.logger import get_logger

logger = get_logger(__name__)


class ReportGenerator:
    """Generates exportable reports from evaluation results."""

    def __init__(self, output_dir: Optional[Path] = None) -> None:
        from config.settings import settings
        self.output_dir = ensure_dir(output_dir or settings.reports_dir)  # type: ignore[arg-type]

    def export_predictions_csv(self, predictions: List[Dict[str, Any]], filename: Optional[str] = None) -> Path:
        filename = filename or f"predictions_{utc_now_iso().replace(':', '-')}.csv"
        path = self.output_dir / filename
        pd.DataFrame(predictions).to_csv(path, index=False)
        return path

    def export_predictions_excel(self, predictions: List[Dict[str, Any]], filename: Optional[str] = None) -> Path:
        filename = filename or f"predictions_{utc_now_iso().replace(':', '-')}.xlsx"
        path = self.output_dir / filename
        pd.DataFrame(predictions).to_excel(path, index=False, engine="openpyxl")
        return path

    def export_predictions_json(self, predictions: List[Dict[str, Any]], filename: Optional[str] = None) -> Path:
        filename = filename or f"predictions_{utc_now_iso().replace(':', '-')}.json"
        path = self.output_dir / filename
        path.write_text(json.dumps(predictions, indent=2, default=str), encoding="utf-8")
        return path

    def export_metrics_csv(self, metrics: Dict[str, Any], filename: Optional[str] = None) -> Path:
        filename = filename or f"metrics_{utc_now_iso().replace(':', '-')}.csv"
        path = self.output_dir / filename
        pd.DataFrame([metrics]).to_csv(path, index=False)
        return path

    def export_pdf(self, title: str, summary: Dict[str, Any], table: List[Dict[str, Any]], filename: Optional[str] = None) -> Path:
        filename = filename or f"report_{utc_now_iso().replace(':', '-')}.pdf"
        path = self.output_dir / filename
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import letter
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.platypus import (
                Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
            )
        except ImportError:
            logger.warning("reportlab not available, falling back to JSON")
            return self.export_predictions_json([summary, *table], filename.replace(".pdf", ".json"))

        doc = SimpleDocTemplate(str(path), pagesize=letter)
        styles = getSampleStyleSheet()
        story = [Paragraph(title, styles["Title"]), Spacer(1, 12)]
        for k, v in summary.items():
            story.append(Paragraph(f"<b>{k}</b>: {v}", styles["Normal"]))
        story.append(Spacer(1, 12))
        if table:
            data = [list(table[0].keys())] + [list(row.values()) for row in table]
            t = Table(data, repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0ea5e9")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]))
            story.append(t)
        doc.build(story)
        return path


__all__ = ["ReportGenerator"]

