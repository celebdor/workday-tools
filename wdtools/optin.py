"""Read Google Form opt-in responses and decide who may have events synced.

Calendar sync is opt-in. The form is expected to have one single-select question
(dropdown, multiple choice, or radio) with the options ``No`` and ``Yes`` and to
collect respondent email addresses. Responses are matched to Workday people
through ``[optin.people]`` (email -> exact Workday display name, or worker id).

Policy: an explicit ``No`` always opts out and an explicit ``Yes`` always opts
in. Everything else — no response, an unmapped person, a response without a
Yes/No answer, or no form configured — follows ``[optin].default``: ``opt_out``
(the built-in, conservative default) or ``opt_in``. The built-in default keeps
the gate fail-closed if the form or mapping is misconfigured.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import Config, render_box
from .models import AbsenceEvent

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/forms.responses.readonly"]

_NO_EMAIL = "(no email)"
_MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)


def _credentials(cfg: Config) -> Credentials:
    creds: Credentials | None = None
    if Path(cfg.google_forms_token_path).exists():
        creds = Credentials.from_authorized_user_file(
            str(cfg.google_forms_token_path), SCOPES
        )
    if creds and creds.valid:
        logger.debug("Using cached Google Forms token at %s", cfg.google_forms_token_path)
        return creds
    if creds and creds.expired and creds.refresh_token:
        logger.info("Refreshing expired Google Forms token.")
        creds.refresh(Request())
    else:
        if not Path(cfg.google_credentials_path).exists():
            raise FileNotFoundError(
                f"Missing Google OAuth client at {cfg.google_credentials_path}. "
                "Run `wdtools setup-google` first."
            )
        logger.info("No valid Forms token; starting Google consent flow in a browser.")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(cfg.google_credentials_path), SCOPES
        )
        creds = flow.run_local_server(port=0)
    Path(cfg.google_forms_token_path).write_text(creds.to_json(), encoding="utf-8")
    return creds


def _service(cfg: Config):
    return build("forms", "v1", credentials=_credentials(cfg), cache_discovery=False)


def authorize(cfg: Config):
    """Run the Google Forms consent flow (writing forms_token.json) and return a service."""
    return _service(cfg)


def check_form_access(cfg: Config) -> str:
    """Confirm the authorized account can read the configured form's responses."""
    if not cfg.optin_form_id:
        state = "opted in" if cfg.optin_default == "opt_in" else "opted out"
        return f"No [optin].form_id configured (sync treats everyone as {state} by default)."
    service = _service(cfg)
    try:
        service.forms().responses().list(
            formId=cfg.optin_form_id, pageSize=1
        ).execute()
    except Exception as exc:
        raise RuntimeError(
            f"Could not read responses from Google Form {cfg.optin_form_id}: {exc}. "
            "Check that the Forms API is enabled, the form ID is correct, and the "
            "authorized account can see the form."
        ) from exc
    return f"Access to Google Form {cfg.optin_form_id} confirmed."


def _normalize(value: str) -> str:
    """Case/whitespace-insensitive key for names and worker ids."""
    return " ".join(value.split()).casefold()


def _normalize_email(value: str) -> str:
    return value.strip().casefold()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable form submission time %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _answer_from_response(payload: dict) -> tuple[bool | None, str]:
    """Return (True for Yes, False for No/ambiguous, None for no Yes/No answer)."""
    values: list[str] = []
    for answer in (payload.get("answers") or {}).values():
        texts = (answer.get("textAnswers") or {}).get("answers") or []
        for text in texts:
            value = str(text.get("value", "")).strip()
            if value:
                values.append(value)

    normalized = {value.casefold() for value in values}
    has_yes = "yes" in normalized
    has_no = "no" in normalized
    raw = ", ".join(values)
    if has_yes and has_no:
        logger.warning(
            "Form response %s contains both Yes and No; treating it as No.",
            payload.get("responseId", "(unknown)"),
        )
        return False, raw
    if has_yes:
        return True, "Yes"
    if has_no:
        return False, "No"
    return None, raw


def _answer_label(response: "OptInResponse") -> str:
    if response.answer is True:
        return "Yes"
    if response.answer is False:
        return "No"
    return f"no Yes/No answer ({response.raw_answer or 'blank'})"


def _sort_key(response: "OptInResponse"):
    """Latest submission wins; ties prefer No, then no answer, then Yes."""
    rank = 0 if response.answer is True else 2 if response.answer is False else 1
    return (response.submitted or _MIN_TIME, rank)


def _format_time(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else "unknown time"


@dataclass(frozen=True)
class OptInResponse:
    email: str
    target: str | None  # mapped Workday display name / worker id, if any
    answer: bool | None  # True = Yes, False = No, None = no Yes/No answer
    raw_answer: str
    submitted: datetime | None


class OptInDirectory:
    """Resolved opt-in decisions, grouped by mapped Workday person."""

    def __init__(
        self,
        form_id: str,
        mapping: dict[str, str],
        responses: list[OptInResponse],
        default: str = "opt_out",
    ) -> None:
        self.form_id = form_id
        self.mapping = dict(mapping)
        self.default = default
        self._mapping_by_email = {
            _normalize_email(email): target for email, target in mapping.items()
        }
        self._targets = {_normalize(target) for target in mapping.values()}
        self.responses = responses
        self.decisions: dict[str, OptInResponse] = {}
        for response in responses:
            if response.target is None:
                continue
            key = _normalize(response.target)
            previous = self.decisions.get(key)
            if previous is None or _sort_key(response) > _sort_key(previous):
                self.decisions[key] = response

    @property
    def configured(self) -> bool:
        return bool(self.form_id)

    @property
    def default_opted_in(self) -> bool:
        return self.default == "opt_in"

    @property
    def unmapped(self) -> list[OptInResponse]:
        return [response for response in self.responses if response.target is None]

    @property
    def opted_in_people(self) -> list[str]:
        return sorted(
            {
                response.target
                for response in self.decisions.values()
                if response.answer is True and response.target
            }
        )

    @property
    def opted_out_people(self) -> list[str]:
        return sorted(
            {
                response.target
                for response in self.decisions.values()
                if response.answer is False and response.target
            }
        )

    @property
    def no_answer_people(self) -> list[str]:
        """Mapped people whose latest response has no Yes/No answer."""
        return sorted(
            {
                response.target
                for response in self.decisions.values()
                if response.answer is None and response.target
            }
        )

    @property
    def no_response_people(self) -> list[str]:
        """Configured people whose email(s) have no response at all."""
        return sorted(
            {
                target
                for target in self.mapping.values()
                if _normalize(target) not in self.decisions
            }
        )

    @property
    def defaulted_people(self) -> list[str]:
        """Mapped people whose effective decision comes from the default."""
        return sorted(
            {
                target
                for target in self.mapping.values()
                if _normalize(target) not in self.decisions
                or self.decisions[_normalize(target)].answer is None
            }
        )

    def target_for_email(self, email: str) -> str | None:
        return self._mapping_by_email.get(_normalize_email(email))

    def _matching_targets(self, person: str, person_id: str | None = None) -> set[str]:
        keys = {_normalize(person)}
        if person_id:
            keys.add(_normalize(person_id))
        return keys & self._targets

    def _decisions_for(
        self, person: str, person_id: str | None = None
    ) -> list[OptInResponse]:
        return [
            self.decisions[target]
            for target in self._matching_targets(person, person_id)
            if target in self.decisions
        ]

    def decision_for(
        self, person: str, person_id: str | None = None
    ) -> OptInResponse | None:
        decisions = self._decisions_for(person, person_id)
        if not decisions:
            return None
        return max(decisions, key=_sort_key)

    def is_person_opted_in(self, person: str, person_id: str | None = None) -> bool:
        decision = self.decision_for(person, person_id)
        if decision is None or decision.answer is None:
            return self.default_opted_in
        return decision.answer

    def is_opted_in(self, event: AbsenceEvent) -> bool:
        return self.is_person_opted_in(event.person, event.person_id)

    def explain(self, person: str, person_id: str | None = None) -> str:
        """Human-readable reason for the current decision (used in logs)."""
        if not self.configured:
            if self.default_opted_in:
                return "no opt-in form configured (opted in by default)"
            return "no opt-in form configured (everyone opted out)"
        if not self._matching_targets(person, person_id):
            if self.default_opted_in:
                return "not mapped in [optin.people] (opted in by default)"
            return "not mapped in [optin.people]"
        decisions = self._decisions_for(person, person_id)
        if not decisions:
            if self.default_opted_in:
                return "no form response (opted in by default)"
            return "no form response (opted out)"
        latest = max(decisions, key=_sort_key)
        if latest.answer is True:
            return f"opted in (Yes from {latest.email})"
        if latest.answer is False:
            return f"opted out (No from {latest.email})"
        if self.default_opted_in:
            return f"no Yes/No answer from {latest.email} (opted in by default)"
        return f"opted out (response from {latest.email} has no Yes/No answer)"

    def as_box(self) -> str:
        summary = [
            ("form_id", self.form_id or "(unset)"),
            ("default", self.default),
            ("responses", len(self.responses)),
            ("opted_in", len(self.opted_in_people)),
            ("opted_out", len(self.opted_out_people)),
            ("no_answer", len(self.no_answer_people)),
            ("no_response", len(self.no_response_people)),
            ("defaulted", len(self.defaulted_people)),
            ("unmapped", len(self.unmapped)),
        ]
        mapping_rows = [
            (email, target) for email, target in sorted(self.mapping.items())
        ]
        answer_rows = [
            (
                response.email,
                f"{_answer_label(response)} -> {response.target or '(unmapped)'}",
            )
            for response in sorted(
                self.responses,
                key=lambda response: (
                    _normalize_email(response.email),
                    _sort_key(response),
                ),
            )
        ]
        decision_rows = [
            (
                response.target or "(unmapped)",
                f"{_answer_label(response)} ({_format_time(response.submitted)})",
            )
            for response in sorted(
                self.decisions.values(),
                key=lambda response: _normalize(response.target or ""),
            )
        ]
        sections: list[tuple[str, list[tuple[str, object]]]] = [("form", summary)]
        if not self.configured:
            everyone = "opted in by default" if self.default_opted_in else "opted out"
            sections.append(("policy", [("everyone", everyone)]))
        if mapping_rows:
            sections.append(("mapping", mapping_rows))
        sections.append(("responses", answer_rows or [("responses", "(none)")]))
        sections.append(("decisions", decision_rows or [("decisions", "(none)")]))
        return render_box("Google Form opt-in", sections)


def _fetch_responses(cfg: Config) -> list[dict]:
    service = _service(cfg)
    responses: list[dict] = []
    page_token: str | None = None
    try:
        while True:
            payload = (
                service.forms()
                .responses()
                .list(
                    formId=cfg.optin_form_id,
                    pageSize=1000,
                    pageToken=page_token,
                )
                .execute()
            )
            responses.extend(payload.get("responses", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:
        raise RuntimeError(
            f"Could not read responses from Google Form {cfg.optin_form_id}: {exc}. "
            "Calendar sync was aborted before making any changes."
        ) from exc
    logger.debug(
        "Fetched %d response(s) from Google Form %s",
        len(responses),
        cfg.optin_form_id,
    )
    return responses


def load_directory(cfg: Config) -> OptInDirectory:
    """Fetch and resolve the opt-in decisions for this sync.

    An empty ``form_id`` applies ``optin_default`` to everyone. A configured
    form that cannot be read raises before the caller makes any calendar
    changes, so a transient API failure can never be mistaken for "nobody opted
    in".
    """
    cfg.validate_optin()
    if not cfg.optin_form_id:
        directory = OptInDirectory("", cfg.optin_people, [], cfg.optin_default)
        _log_directory(directory)
        return directory

    mapping_by_email = {
        _normalize_email(email): target
        for email, target in cfg.optin_people.items()
    }
    responses: list[OptInResponse] = []
    for payload in _fetch_responses(cfg):
        email = str(payload.get("respondentEmail") or "").strip()
        target = mapping_by_email.get(_normalize_email(email)) if email else None
        answer, raw_answer = _answer_from_response(payload)
        responses.append(
            OptInResponse(
                email=email or _NO_EMAIL,
                target=target,
                answer=answer,
                raw_answer=raw_answer,
                submitted=_parse_time(payload.get("lastSubmittedTime")),
            )
        )

    directory = OptInDirectory(
        cfg.optin_form_id, cfg.optin_people, responses, cfg.optin_default
    )
    _log_directory(directory)
    return directory


def _log_directory(directory: OptInDirectory) -> None:
    if not directory.configured:
        if directory.default_opted_in:
            logger.warning(
                "No [optin].form_id configured: treating everyone as opted in by "
                "default. No form responses can opt anyone out."
            )
            logger.info(
                "Opt-in summary: no form configured; everyone is opted in by default."
            )
        else:
            logger.warning(
                "No [optin].form_id configured: treating everyone as opted out. "
                "Existing workday-managed calendar events inside the sync window "
                "will be removed."
            )
            logger.info(
                "Opt-in summary: no form configured; everyone is opted out by policy."
            )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Resolved opt-in responses:\n%s", directory.as_box())
        return

    if not directory.responses:
        state = "opted in" if directory.default_opted_in else "opted out"
        logger.warning(
            "Google Form %s has no responses yet: everyone is %s by default.",
            directory.form_id,
            state,
        )

    for response in directory.responses:
        logger.info(
            "Opt-in response: %s = %s (%s)",
            response.email,
            _answer_label(response),
            response.target or "unmapped",
        )
        if not response.target:
            logger.warning(
                "Opt-in response from %s has no [optin.people] mapping; ignoring it.",
                response.email,
            )

    logger.info(
        "Opt-in summary: %d response(s), %d opted in, %d opted out, "
        "%d no response, %d no Yes/No answer, %d unmapped; default=%s.",
        len(directory.responses),
        len(directory.opted_in_people),
        len(directory.opted_out_people),
        len(directory.no_response_people),
        len(directory.no_answer_people),
        len(directory.unmapped),
        directory.default,
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Resolved opt-in responses:\n%s", directory.as_box())
