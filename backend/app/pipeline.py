"""The order in which a claimed document is processed."""

from app.ap.records import records_step
from app.extraction.extract import extract_step
from app.intake.steps import read_pages
from app.matching.match import match_step

STEPS = [read_pages, extract_step, records_step, match_step]  # matching runs the checks in the same transaction
