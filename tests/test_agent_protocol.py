"""One complete offline Responses exchange through HTTP and the real Studio tools."""

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from argos_studio.agent_provider import AgentConfig, OpenAIProvider
from argos_studio.app import Settings, create_app


class OfflineResponsesDouble(OpenAIProvider):
    """Use the production adapter with scripted HTTP responses, never a model."""

    provider = "test-double"
    sends_data_off_machine = False


def test_http_agent_uses_responses_tools_to_persist_report_and_unexecuted_proposal(tmp_path):
    placeholder_key = "protocol-test-placeholder-not-an-api-key"
    requests, responses, evidence = [], [], {}

    def function_output(number, name, arguments):
        return [
            {
                "id": f"reasoning-{number}",
                "type": "reasoning",
                "summary": [],
                "encrypted_content": f"opaque-protocol-fixture-{number}",
            },
            {
                "id": f"function-{number}",
                "type": "function_call",
                "status": "completed",
                "call_id": f"call-{number}",
                "name": name,
                "arguments": json.dumps(arguments),
            },
        ]

    def handler(request):
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == f"Bearer {placeholder_key}"
        body = json.loads(request.content)
        requests.append(body)
        number = len(requests)
        assert body["store"] is False
        assert body["parallel_tool_calls"] is False
        assert all(tool["type"] == "function" and tool["strict"] for tool in body["tools"])
        assert "start_experiment" not in {tool["name"] for tool in body["tools"]}
        assert body["model"] == "offline-protocol-fixture"
        assert placeholder_key not in request.content.decode()
        if number == 1:
            initial = json.loads(body["input"][0]["content"])
            assert initial["session_context"]["session"]["id"] == sid
            assert initial["window_s"] == {"start_s": 0.1, "end_s": 0.5}
            output = function_output(
                1,
                "investigate_reception",
                {"start_s": None, "end_s": None, "context": "Synthetic protocol test"},
            )
        else:
            # The full preceding response, including opaque reasoning, must be
            # replayed before its matching exact tool output. That result must
            # already be durable when the next provider request is made.
            previous = requests[-2]["input"]
            tool_item = body["input"][-1]
            assert body["input"] == [*previous, *responses[-1], tool_item]
            assert tool_item["type"] == "function_call_output"
            assert tool_item["call_id"] == f"call-{number - 1}"
            result = json.loads(tool_item["output"])
            active = store.list_agent_runs(sid)[0]
            trace = store.get_agent_run(sid, active["id"])["steps"]
            retained = next(
                step["payload"]
                for step in trace
                if step["kind"] == "tool_result"
                and step["payload"]["call_id"] == tool_item["call_id"]
            )
            assert retained["ok"] is True
            assert retained["result"] == result
            if number == 2:
                evidence["report_id"] = result["id"]
                report = store.get_investigation(sid, result["id"])
                assert report["window_s"] == {"start_s": 0.1, "end_s": 0.5}
                assert report["snapshot"]["sha256"] == result["snapshot"]["sha256"]
                output = function_output(
                    2, "prepare_synthetic_experiment", {"report_id": result["id"]}
                )
            else:
                assert number == 3
                evidence["experiment_id"] = result["id"]
                proposal = store.get_experiment(result["id"])
                assert proposal["investigation_id"] == evidence["report_id"]
                assert result["execution_started"] is False
                assert proposal["status"] == "proposed"
                output = [
                    {
                        "id": "final-message",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    f"Rapport synthétique {evidence['report_id']} enregistré. "
                                    f"Proposition {result['id']} préparée ; aucun essai lancé."
                                ),
                                "annotations": [],
                            }
                        ],
                    }
                ]
        responses.append(output)
        return httpx.Response(
            200,
            json={
                "id": f"response-{number}",
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": number * 100, "output_tokens": number * 10},
            },
        )

    provider = OfflineResponsesDouble(
        AgentConfig(provider="openai", model="offline-protocol-fixture", api_key=placeholder_key),
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(data_dir=tmp_path, agent_config=AgentConfig())
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        store = client.app.state.store
        sid = store.create_session("Synthetic protocol fixture", "Inspect one receipt gap")["id"]
        for elapsed in (0, 0.05, 0.55, 0.6):
            store.append_sample(
                sid,
                elapsed_s=elapsed,
                source_time_s=10 + elapsed,
                received_at=1000 + elapsed,
                roll_deg=0,
                pitch_deg=0,
                gyro_x_deg_s=None,
            )
        store.finish_session(sid)
        before = store.snapshot(sid)
        health = client.get("/api/health").json()["agent"]
        assert health["provider"] == "test-double"
        assert health["model"] == "offline-protocol-fixture"
        assert health["sends_data_off_machine"] is False
        prefix = f"/api/sessions/{sid}/agent-runs"
        started = client.post(
            prefix,
            json={
                "prompt": "Examine cette fenêtre et prépare un essai synthétique sans le lancer.",
                "start_s": 0.1,
                "end_s": 0.5,
            },
        )
        assert started.status_code == 202, started.text
        detail = f"{prefix}/{started.json()['id']}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            finished = client.get(detail).json()
            if finished["status"] != "running":
                break
            time.sleep(0.01)
        else:
            pytest.fail("Offline protocol execution did not finish")
        assert finished["status"] == "completed", finished
        assert len(requests) == 3
        assert finished["provider"] == "test-double"
        assert evidence["report_id"] in finished["answer"]
        assert evidence["experiment_id"] in finished["answer"]
        assert finished["usage"] == {
            "input_tokens": 600,
            "output_tokens": 60,
            "complete": True,
            "rounds": 3,
            "tool_calls": 3,
        }
        exported = client.get(detail + "/export")
        assert exported.status_code == 200
        assert exported.json() == finished
        assert "attachment" in exported.headers["content-disposition"]
        for private in (placeholder_key, "opaque-protocol-fixture", "encrypted_content"):
            assert private not in exported.text
        report = client.get(f"/api/sessions/{sid}/investigations/{evidence['report_id']}")
        proposal = client.get(f"/api/experiments/{evidence['experiment_id']}").json()
        assert report.status_code == 200
        assert proposal["status"] == "proposed"
        assert proposal["control_session_id"] is proposal["perturbed_session_id"] is None
        assert len(store.list_investigations(sid)) == len(store.list_experiments()) == 1
        assert len(store.list_sessions()) == 1
        assert store.snapshot(sid) == before
        assert not client.app.state.runtime.active
        assert not client.app.state.experiments.active

    # Reopening does not need the scripted provider and never replays its calls.
    with TestClient(create_app(settings)) as reopened:
        assert reopened.get(detail + "/export").json() == finished
        assert len(requests) == 3
