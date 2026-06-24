import os
import json
import logging
import re
import hmac
import hashlib
import requests
from typing import Dict, Any, Optional

logger = logging.getLogger("hermes.plugin.compliance_critic")

SESSION_RESOLVER_URL = os.getenv("SESSION_RESOLVER_URL", "http://platform-agent.agent-system.svc.cluster.local:8699")


def normalize_schedule(s: str) -> str:
    """Normalize common invalid schedule strings (like 'immediate' or seconds) to valid formats."""
    s = s.strip().lower()
    if s in ("immediate", "now", "0s", "0m"):
        return "1m"
    
    # Match seconds format (e.g., '30s', '60 seconds') and convert to minutes (rounded up)
    match = re.match(r'^(\d+)\s*(s|sec|secs|second|seconds)$', s)
    if match:
        val = int(match.group(1))
        mins = (val + 59) // 60
        return f"{mins}m"
    return s


def fetch_metadata_from_session_store(session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve session metadata from the KV store API (port 8699)."""
    if not session_id:
        return None
    url = f"{SESSION_RESOLVER_URL.rstrip('/')}/v1/sessions/{session_id}/metadata"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.debug(f"Failed to fetch session metadata from KV store: {e}")
        return None


def send_chat_notification(space_id: str, thread_id: str, message: str):
    """Post a notification directly to the Google Chat space via the webhook server (port 8644)."""
    if not space_id or not space_id.startswith("spaces/"):
        return
    url = "http://platform-agent.agent-system.svc.cluster.local:8644/webhooks/swarm-notification"
    worker_id = os.getenv("OTEL_SERVICE_NAME") or os.getenv("HOSTNAME") or "compliance_critic"
    # Clean worker ID
    worker_id = re.sub(r'[^a-zA-Z0-9_\-]', '-', worker_id)
    # Strip pod hashes
    worker_id = re.sub(r'-[a-z0-9]{8,10}-[a-z0-9]{5}$', '', worker_id)
    payload = {
        "worker_id": worker_id,
        "user_space": space_id,
        "user_thread": thread_id,
        "message": message
    }
    try:
        body_bytes = json.dumps(payload).encode("utf-8")
        sig = hmac.new(b"k8s-swarm-secret-999", body_bytes, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig
        }
        response = requests.post(url, data=body_bytes, headers=headers, timeout=2.0)
        response.raise_for_status()
    except Exception as e:
        logger.warning("Failed to send compliance notification to webhook: %s", e)


def register(ctx):
    llm = ctx.llm

    def run_critic_and_schedule(
        session_id: str,
        user_message: str,
        assistant_response: str,
        model: str,
        platform: str,
        **kwargs,
    ) -> None:
        """
        Programmatic Critic: Analyzes the final response to check if an async wait
        is required and if a cronjob was scheduled. If missing, it schedules it.
        """
        try:
            import pathlib
            prompt_path = pathlib.Path(__file__).parent / "TASK_SUCCESS_CRITERIA.md"
            if prompt_path.exists():
                critic_prompt = prompt_path.read_text(encoding="utf-8")
            else:
                logger.warning("TASK_SUCCESS_CRITERIA.md not found, using default fallback prompt")
                critic_prompt = "Verify if response is compliant with turn completion constraint."

            schema = {
                "type": "object",
                "properties": {
                    "is_async_or_pending": {"type": "boolean"},
                    "is_compliant": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "recommended_followup_prompt": {"type": "string"},
                    "recommended_schedule": {"type": "string"}
                },
                "required": ["is_async_or_pending", "is_compliant", "reason"]
            }

            # Build input for structured evaluation
            input_parts = []
            if user_message:
                input_parts.append(f"User Request:\n{user_message}")
            input_parts.append(f"Proposed Response:\n{assistant_response}")
            evaluation_input = "\n\n".join(input_parts)

            logger.info("Compliance Critic running structured evaluation...")
            result = llm.complete_structured(
                instructions=critic_prompt,
                input=[{"type": "text", "text": evaluation_input}],
                json_schema=schema
            )

            parsed_result = result.parsed
            if not isinstance(parsed_result, dict):
                logger.error(f"Critic failed to return a valid JSON object. Parsed: {parsed_result}")
                return

            is_async_waiting = parsed_result.get("is_async_or_pending", False)
            is_compliant = parsed_result.get("is_compliant", True)
            reason = parsed_result.get("reason", "")

            logger.info(f"Critic Evaluation: async_waiting={is_async_waiting}, compliant={is_compliant}, reason='{reason}'")

            # Import cron engine dynamically
            from cron.jobs import create_job, list_jobs, update_job, remove_job
            from gateway.session_context import get_session_env

            # Fetch chat context over HTTP from KV store
            metadata = fetch_metadata_from_session_store(session_id)
            chat_id = None
            thread_id = None
            if metadata:
                chat_id = metadata.get("google_chat_id")
                thread_id = metadata.get("google_thread_id")

            # Fallback to env if metadata fetch failed
            if not chat_id:
                chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
            if not thread_id:
                thread_id = get_session_env("HERMES_SESSION_THREAD_ID")

            # Check for existing pending follow-up job for this session
            existing_job = None
            try:
                for j in list_jobs(include_disabled=True):
                    if j.get("name") == f"fup_{session_id}":
                        existing_job = j
                        break
            except Exception as list_err:
                logger.warning(f"Failed to list existing cron jobs: {list_err}")

            if not is_compliant:
                followup_prompt = parsed_result.get("recommended_followup_prompt")
                schedule = parsed_result.get("recommended_schedule", "1m")
                schedule = normalize_schedule(schedule)

                if not followup_prompt:
                    followup_prompt = f"Check status of pending operation in session {session_id} and continue execution."

                logger.warning(f"Critic detected missing cronjob for pending async state! Scheduling programmatically...")

                origin = None
                if chat_id:
                    origin = {
                        "platform": platform or "google_chat",
                        "chat_id": chat_id,
                    }
                    if thread_id:
                        origin["thread_id"] = thread_id

                # Get existing retry count from the job dict
                retry_count = 0
                if existing_job:
                    retry_count = existing_job.get("retry_count", 0)

                # Check limit (max 3 retries)
                if retry_count >= 3:
                    logger.warning(f"Session {session_id} has already been rescheduled {retry_count} times. Stopping to prevent loops.")
                    if chat_id:
                        send_chat_notification(
                            space_id=chat_id,
                            thread_id=thread_id,
                            message="⚠️ *Response Compliance Guard*: Maximum automatic retry limit (3) reached. Stopping background checks."
                        )
                    # We do NOT update the job. Since repeat=2 and completed=1 (or 2), mark_job_run will clean it up.
                    return

                # Extract provider/model from current execution model
                prov, model_name = None, None
                if model and "/" in model:
                    prov, model_name = model.split("/", 1)
                else:
                    model_name = model

                new_retry_count = retry_count + 1
                job_id = None

                if existing_job:
                    logger.warning(f"Critic detected existing pending follow-up job {existing_job['id']}. Rescheduling it (attempt {new_retry_count}/3)...")
                    try:
                        updated_job = update_job(
                            existing_job["id"],
                            {
                                "prompt": followup_prompt,
                                "repeat": {"times": 2, "completed": 0},  # Reset completed count, set times=2 so it survives mark_job_run
                                "schedule": schedule,
                                "state": "scheduled",
                                "enabled": True,
                                "retry_count": new_retry_count
                            }
                        )
                        if updated_job:
                            job_id = updated_job["id"]
                    except Exception as update_err:
                        logger.error(f"Failed to update existing cron job: {update_err}")

                if not job_id:
                    # Create a new job with repeat=2 so it survives one execution run
                    job = create_job(
                        prompt=followup_prompt,
                        schedule=schedule,
                        name=f"fup_{session_id}",
                        repeat=2,
                        provider=prov,
                        model=model_name,
                        origin=origin,
                        session_id=session_id,
                    )
                    job_id = job["id"]
                    # Add retry_count parameter
                    update_job(job_id, {"retry_count": new_retry_count})
                    logger.info(f"Successfully scheduled new follow-up job: {job_id} (attempt {new_retry_count}/3)")

                # Send Google Chat notification
                if chat_id:
                    state_desc = "pending asynchronous operations" if is_async_waiting else "incomplete turn end-state"
                    send_chat_notification(
                        space_id=chat_id,
                        thread_id=thread_id,
                        message=f"⚠️ *Response Compliance Guard*: Detected {state_desc}. Automatically scheduled follow-up check in *{schedule}* (attempt {new_retry_count}/3)."
                    )

            else:  # is_compliant is True
                # If there's a pending follow-up job, remove it since the session is now compliant
                if existing_job:
                    try:
                        remove_job(existing_job["id"])
                        logger.info(f"Removed pending follow-up job {existing_job['id']} since session is now compliant.")
                    except Exception as remove_err:
                        logger.warning(f"Failed to clean up pending follow-up job: {remove_err}")

        except Exception as e:
            logger.error(f"Compliance Critic error in post_llm_call hook: {e}", exc_info=True)

    ctx.register_hook("post_llm_call", run_critic_and_schedule)
