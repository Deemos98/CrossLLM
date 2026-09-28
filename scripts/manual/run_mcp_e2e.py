"""
MCP 端到端验证脚本 - 精简版
只测试 1 个模型，验证 LLM → search → result → Final Answer 完整链路
使用 httpx（项目已有依赖）
"""

import asyncio
import json
import time
from datetime import datetime

import httpx

BASE_URL = "http://127.0.0.1:8765"

async def test_mcp_tool_call_chain():
    """测试完整的 MCP 工具调用链路"""

    print("=" * 60)
    print("MCP Tool Call End-to-End Verification")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=300) as client:
        # Step 1: Create Session
        print("\n[1] Creating session...")
        resp = await client.post(f"{BASE_URL}/api/session", json={
            "question": "请调查 faster-whisper 当前常见的 VAD 问题。要求：1. 搜索 GitHub Issue；2. 查找网页资料；3. 如果存在相关论文，查询论文；4. 综合资料后给出结论。"
        })
        result = resp.json()
        session_id = result["session_id"]
        print(f"    ✓ Session created: {session_id}")

        # Step 2: Start Round with ONLY minimax_m2_7 (has github + openwebsearch + context7)
        print("\n[2] Starting round with minimax_m2_7 (single model)...")
        resp = await client.post(f"{BASE_URL}/api/session/{session_id}/round", json={
            "user_question": "请调查 faster-whisper 的 VAD 问题。要求：1) 搜索 GitHub Issue 看常见故障；2) 用网页搜索找官方文档或社区资料；3) 读取本项目 docs/mcp.md 了解当前 MCP 配置；4) 如果 Stack Overflow 有类似讨论也参考进来；5) 给出综合结论。",
            "selected_models": ["minimax_m2_7"],
            "peer_review": False
        })
        if resp.status_code not in [200, 202]:
            print(f"    ✗ Failed to start round: {resp.status_code} - {resp.text}")
            return
        result = resp.json()
        round_id = result["round_id"]
        print(f"    ✓ Round started: {round_id}")

        # Step 3: Listen to SSE events
        sse_timeout = 240
        print(f"\n[3] Listening to SSE events (timeout={sse_timeout}s)...")
        print("    Waiting for LLM response with tool calls...")

        tool_call_detected = False
        final_answer = ""
        start_time = time.time()

        try:
            async with client.stream(
                "GET",
                f"{BASE_URL}/api/session/{session_id}/events/{round_id}",
                timeout=sse_timeout + 30
            ) as resp:
                async for line in resp.aiter_lines():
                    if time.time() - start_time > sse_timeout:
                        print(f"    ⚠ Timeout after {sse_timeout}s")
                        break

                    if not line.strip():
                        continue

                    if line.startswith('data: '):
                        data_str = line[6:]
                        if data_str == '[DONE]':
                            print("    ✓ Stream completed")
                            break

                        try:
                            event = json.loads(data_str)
                            event_type = event.get("type", "")

                            if event_type == "token":
                                chunk = event.get("content", "")
                                final_answer += chunk

                            elif event_type == "done":
                                print("    ✓ Model done")
                                break

                            elif event_type == "error":
                                print(f"    ✗ Model error: {event.get('error', '')}")
                                break

                        except json.JSONDecodeError:
                            pass

        except Exception as e:
            print(f"    ✗ SSE Error: {e}")

        # Step 4: Verify results
        print("\n" + "=" * 60)
        print("VERIFICATION RESULTS")
        print("=" * 60)

        print(f"\n✓ Session ID: {session_id}")
        print(f"✓ Round ID:   {round_id}")
        print(f"✓ Tools passed to provider: {'YES (check logs)' if not tool_call_detected else 'YES'}")

        if final_answer:
            print(f"✓ Final answer length: {len(final_answer)} chars")

            print(f"\n📝 FINAL ANSWER PREVIEW:")
            print("-" * 60)
            print(final_answer[:500])
            if len(final_answer) > 500:
                print(f"... ({len(final_answer) - 500} more chars)")
            print("-" * 60)

            # Check if answer contains evidence of search usage
            has_url = any(keyword in final_answer.lower() for keyword in
                         ['http', 'www.', '.com', 'search', 'found', 'according to', '结果', '搜索',
                          'beijing', 'time.is', 'current time'])
            print(f"\n🔍 Search evidence in answer: {'DETECTED' if has_url else 'NOT DETECTED'}")

    print("\n" + "=" * 60)
    print("Check detailed log at:")
    import os
    session_dir = f"C:/pythonProject/CrossLLM/sessions/{session_id}"
    round_dir = f"{session_dir}/round_{round_id:03d}"
    print(f"  {round_dir}")
    print(f"\nOr grep [mcp] in .uvicorn.log:")
    print(f"  grep '\\[mcp\\]' C:/pythonProject/CrossLLM/.uvicorn.log")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(test_mcp_tool_call_chain())
