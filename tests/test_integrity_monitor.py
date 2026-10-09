"""v2 Monitor HMAC jobs with synthetic imports and finite mocked requests."""
import copy
import json
import os
import time
import unittest
from unittest.mock import patch

from tests import test_monitor_internal as monitor_fixture
from tests.test_kbf_review import synthetic_reference
from tests.integrity_fixtures import account_evidence
from features.integrity import service, monitor_adapter
from features.model_coverage.monitor import MonitorStore


class IntegrityMonitorTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = monitor_fixture.MonitorContractTests.asyncSetUp
    call = monitor_fixture.MonitorContractTests.call
    inventory = monitor_fixture.MonitorContractTests.inventory

    async def asyncTearDown(self):
        await monitor_adapter.stop_offline_executor()
        service.configure_monitor_resolver(None)
        await monitor_fixture.MonitorContractTests.asyncTearDown(self)

    def evidence_body(self):
        return {"schema_version":"2.0", "job_type":"nerfed-evidence-analysis",
                "evidence":account_evidence(), "confirm_authorized":True}

    async def test_nerfed_api_is_new_async_type_and_preserves_offline_contract(self):
        from features.integrity.unified import UnifiedService
        inventory=self.inventory();inventory["channels"][0]["models"]=["gpt-6-astra"]
        MonitorStore(self.registry).import_inventory("cfg-1",inventory,{})
        body={"schema_version":"2.0","job_type":"nerfed-api","confirm_live":True,
              "target":{"channel_identity":"newapi-channel-96","inventory_version":"cfg-1","model":"gpt-6-astra","protocol":"responses"}}
        response=await self.call("POST","/internal/v1/integrity-jobs",body,idempotency="nerfed-api-synthetic")
        self.assertEqual(response.status_code,202,response.text)
        job=response.json()["job"];self.assertEqual(job["consumed"]["requests"],0)
        calls=[]
        async def send(channel,probe,*,before_send):
            await before_send();calls.append(probe["id"])
            return {"status":"completed","text":"OK" if probe["max_tokens"]==32 else " ".join(["42"]*331)}
        with patch.dict(os.environ,{"EVAL_INTEGRITY_EXECUTOR":"live"}):
            await UnifiedService(self.registry).run_pending(send)
        result=await self.call("GET",f"/internal/v1/integrity-jobs/{job['task_id']}/result")
        self.assertEqual(result.json()["job"]["status"],"completed")
        self.assertEqual(len(calls),4)
        self.assertEqual(result.json()["job"]["reports"][0]["method"],"nerfed-api")
        self.assertEqual(result.json()["job"]["reports"][0]["metadata_status"],"unavailable")
        self.assertEqual((await self.call("POST","/internal/v1/integrity-jobs",self.evidence_body())).status_code,202)

    async def test_offline_async_idempotency_result_zero_network_and_principal(self):
        response = await self.call("POST", "/internal/v1/integrity-jobs", self.evidence_body(), idempotency="evidence-v2")
        self.assertEqual(response.status_code, 202, response.text)
        task = response.json()["job"]
        self.assertEqual((task["status"],task["outbound_requests"]), ("queued",0))
        replay = await self.call("POST", "/internal/v1/integrity-jobs", self.evidence_body(), idempotency="evidence-v2")
        self.assertEqual(replay.json()["job"]["job_id"],task["job_id"])
        changed = self.evidence_body(); changed["evidence"]["account_alias"] = "another-synthetic-account"
        conflict = await self.call("POST", "/internal/v1/integrity-jobs", changed, idempotency="evidence-v2")
        self.assertEqual((conflict.status_code,conflict.json()["error"]["code"]), (409,"idempotency_conflict"))
        await monitor_adapter.run_offline_pending()
        result = await self.call("GET",f"/internal/v1/integrity-jobs/{task['job_id']}/result")
        self.assertEqual(result.json()["job"]["status"],"completed")
        self.assertEqual(result.json()["job"]["results"][0]["metadata_status"],"unavailable")
        self.assertEqual(result.json()["job"]["outbound_requests"],0)
        own = monitor_adapter.submit_evidence(account_evidence(),principal="workbench",idempotency_key="local-v2",confirm_authorized=True)
        self.assertEqual((await self.call("GET",f"/internal/v1/integrity-jobs/{own['job_id']}")).status_code,404)

    async def test_basic_login_nonce_and_body_signature_are_not_substitutes(self):
        self.assertEqual((await self.client.post("/internal/v1/integrity-jobs",json=self.evidence_body())).status_code,403)
        nonce = "synthetic-nonce-123456789"
        first = await self.call("POST","/internal/v1/integrity-jobs",self.evidence_body(),nonce=nonce)
        self.assertEqual(first.status_code,202)
        repeat = await self.call("POST","/internal/v1/integrity-jobs",self.evidence_body(),nonce=nonce)
        self.assertEqual(repeat.json()["error"]["code"],"replayed_request")
        signed = await self.call("POST","/internal/v1/integrity-jobs",self.evidence_body(),secret="wrong-synthetic-secret")
        self.assertEqual(signed.status_code,401)
        with patch.dict(os.environ,{"PLATFORM_USERNAME":"synthetic","PLATFORM_PASSWORD":"synthetic-login-password"}):
            from workbench import create_app
            import httpx
            previous = self.client
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),base_url="http://testserver") as protected:
                self.client=protected
                denied=await self.call("GET","/api/registry/channels")
            self.client=previous
        self.assertEqual(denied.status_code,401)

    async def test_version_whitelist_and_legacy_active_nerfed_errors(self):
        for body, status, code in [({"strategy":"is-gpt-nerfed"},422,"strategy_contract_changed"),
                ({"schema_version":"1.0"},400,"schema_version_unsupported"),
                ({**self.evidence_body(),"file_path":"synthetic-path"},400,"invalid_field")]:
            response=await self.call("POST","/internal/v1/integrity-jobs",body)
            self.assertEqual((response.status_code,response.json()["error"]["code"]),(status,code))
        body=self.evidence_body();body["evidence"]["events"]=[{"type":"turn_context","content":"synthetic forbidden body"}]
        response=await self.call("POST","/internal/v1/integrity-jobs",body)
        self.assertEqual(response.status_code,422)

    async def test_cancel_resume_keeps_offline_identity_and_executes_once(self):
        response=await self.call("POST","/internal/v1/integrity-jobs",self.evidence_body())
        job=response.json()["job"]; path=f"/internal/v1/integrity-jobs/{job['job_id']}"
        self.assertEqual((await self.call("POST",path+"/cancel",{})).json()["job"]["status"],"cancelled")
        self.assertEqual(await monitor_adapter.run_offline_pending(),0)
        self.assertEqual((await self.call("POST",path+"/resume",{})).json()["job"]["deadline"],job["deadline"])
        self.assertEqual(await monitor_adapter.run_offline_pending(),1)
        self.assertEqual(await monitor_adapter.run_offline_pending(),0)

    async def test_active_review_binds_inventory_and_uses_real_consumer(self):
        inventory=self.inventory();inventory["channels"][0]["models"]=["gpt-6-astra"]
        store=MonitorStore(self.registry);store.import_inventory("cfg-1",inventory,{})
        package=synthetic_reference(count=4)
        svc=service.ReviewService(self.registry)
        svc.import_reference(package,package["package_hash"],principal="monitor",confirm_authorized=True)
        b=package["budget"]
        body={"schema_version":"2.0","job_type":"active-review",
              "target":{"channel_identity":"newapi-channel-96","inventory_version":"cfg-1","model":"gpt-6-astra","protocol":"responses"},
              "review":{"strategy_id":"kbf","reference_hash":package["package_hash"],"source_ref":"synthetic-monitor-incident", "incident_id":"synthetic-v2",
                        "limits":{k:b[k] for k in ("max_requests","max_input_tokens","max_output_tokens")},
                        "budget_seconds":b["total_timeout_seconds"],"conditions":package["self_test"]["conditions"],"confirm_live":True}}
        response=await self.call("POST","/internal/v1/integrity-jobs",body,idempotency="active-v2")
        self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(response.json()["job"]["consumed"]["requests"],0)
        calls=[]
        async def send(channel,probe,*,before_send):
            await before_send();calls.append(probe["id"])
            return {"status":"completed","text":"1","valid":True}
        with patch.dict(os.environ,{"EVAL_INTEGRITY_EXECUTOR":"live"}):
            await svc.run_pending(send)
        final=await self.call("GET",f"/internal/v1/integrity-jobs/{response.json()['job']['task_id']}/result")
        self.assertEqual((final.json()["job"]["status"],len(calls)),("completed",4))
        self.assertEqual(final.json()["job"]["fees"]["estimated_usd"],None)
        bad=copy.deepcopy(body);bad["target"]["model"]={}
        self.assertEqual((await self.call("POST","/internal/v1/integrity-jobs",bad)).status_code,400)
        overridden=copy.deepcopy(body);overridden["review"]["registry_channel_id"]=999
        self.assertEqual((await self.call("POST","/internal/v1/integrity-jobs",overridden)).status_code,400)
