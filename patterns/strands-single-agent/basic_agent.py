import json
import os
import traceback

import boto3
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from strands_code_interpreter import StrandsCodeInterpreterTools

from utils.auth import extract_user_id_from_context, get_gateway_access_token
from utils.principal import resolve_principal_from_context, MembershipError
from utils.role import get_user_role_from_context
from utils.ssm import get_ssm_parameter

app = BedrockAgentCoreApp()


def create_gateway_mcp_client(access_token: str) -> MCPClient:
    """
    Create MCP client for AgentCore Gateway with OAuth2 authentication.

    MCP (Model Context Protocol) is how agents communicate with tool providers.
    This creates a client that can talk to the AgentCore Gateway using the provided
    access token for authentication. The Gateway then provides access to Lambda-based tools.
    """
    stack_name = os.environ.get("STACK_NAME")
    if not stack_name:
        raise ValueError("STACK_NAME environment variable is required")

    # Validate stack name format to prevent injection
    if not stack_name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid STACK_NAME format")

    print(f"[AGENT] Creating Gateway MCP client for stack: {stack_name}")

    # Fetch Gateway URL from SSM
    gateway_url = get_ssm_parameter(f"/{stack_name}/gateway_url")
    print(f"[AGENT] Gateway URL from SSM: {gateway_url}")

    # Create MCP client with Bearer token authentication
    gateway_client = MCPClient(
        lambda: streamablehttp_client(
            url=gateway_url, headers={"Authorization": f"Bearer {access_token}"}
        ),
        prefix="gateway",
    )

    print("[AGENT] Gateway MCP client created successfully")
    return gateway_client


def create_basic_agent(
    user_id: str,
    session_id: str,
    user_role: str = "EXTERNAL",
    org_id: str = "",
) -> Agent:
    """
    Create a basic agent with Gateway MCP tools and memory integration.

    Args:
        user_id:   Cognito sub claim — immutable user identity.
        session_id: Runtime session ID for memory continuity.
        user_role: "INTERNAL" or "EXTERNAL" — controls citation format.
        org_id:    Organisation ID — tenantId for all RAG retrieve calls.
                   MUST be org_id, not user_id (Phase 4 org-level tenancy).
    """
    # tenantId passed to tools is org_id (not user sub)
    tenant_id = org_id if org_id else user_id  # fallback for safety

    # Role-aware citation format instruction
    if user_role == "INTERNAL":
        citation_format_instruction = """CITATION FORMAT (use exactly — internal user, full citations):
  [Source: <file_name>, docId:<doc_id>, page <page_number>, chunk <X>/<Y>]
  Example: "The encoder maps input to a continuous representation [Source: attention-paper.pdf, docId:abc-123, page 3, chunk 2/8]."
  The docId field enables the Source Viewer. Always include it when present in the retrieved context."""
        retrieval_role_instruction = (
            f'Always pass tenantId="{tenant_id}", userId="{user_id}", '
            f'and userRole="INTERNAL" when calling rag_retrieve_documents.'
        )
    else:
        citation_format_instruction = """CITATION FORMAT (use exactly — external user, masked citations):
  Answer the question based on the retrieved knowledge. Do not expose document names, file structures, or knowledge base organization.
  When asked for sources, you may say "Based on our knowledge base" without revealing specific document details."""
        retrieval_role_instruction = (
            f'Always pass tenantId="{tenant_id}", userId="{user_id}", '
            f'and userRole="EXTERNAL" when calling rag_retrieve_documents.'
        )

    system_prompt = f"""You are a helpful assistant with access to the user's uploaded documents and a Code Interpreter.

DOCUMENT RETRIEVAL RULES:
1. Whenever the user asks a question that could be answered by their uploaded documents, you MUST call the rag_retrieve_documents tool FIRST before composing your answer.
2. {retrieval_role_instruction}
3. If the tool returns a non-empty context_block, base your answer on the returned passages and cite every source inline using the format below.
4. If the tool returns chunks_found = 0 or an empty context_block, answer from your general knowledge and do NOT fabricate or imply any document source.
5. Never claim a document says something that is not present verbatim in the returned context_block.
6. If the user's question spans multiple topics, call rag_retrieve_documents once with the full question — the tool retrieves the most relevant passages across all of the user's documents automatically.

{citation_format_instruction}

CODE INTERPRETER:
Use the execute_python_securely tool when the user asks for calculations, data analysis, chart generation, or any task that benefits from running Python code.

GENERAL BEHAVIOUR:
- Be concise and factual. Do not pad answers with filler.
- When listing your available tools, describe rag_retrieve_documents, execute_python_securely, and any other Gateway tools you discover."""

    bedrock_model = BedrockModel(
        model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0", temperature=0.1
    )

    memory_id = os.environ.get("MEMORY_ID")
    if not memory_id:
        raise ValueError("MEMORY_ID environment variable is required")

    # Configure AgentCore Memory
    agentcore_memory_config = AgentCoreMemoryConfig(
        memory_id=memory_id, session_id=session_id, actor_id=user_id
    )

    session_manager = AgentCoreMemorySessionManager(
        agentcore_memory_config=agentcore_memory_config,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )

    # Initialize Code Interpreter tools with boto3 session
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    code_tools = StrandsCodeInterpreterTools(region)

    try:
        print("[AGENT] Starting agent creation with Gateway tools...")

        # Get OAuth2 access token and create Gateway MCP client
        print("[AGENT] Step 1: Getting OAuth2 access token...")
        access_token = get_gateway_access_token()
        print(f"[AGENT] Got access token: {access_token[:20]}...")

        # Create Gateway MCP client with authentication
        print("[AGENT] Step 2: Creating Gateway MCP client...")
        gateway_client = create_gateway_mcp_client(access_token)
        print("[AGENT] Gateway MCP client created successfully")

        print(
            "[AGENT] Step 3: Creating Agent with Gateway tools and Code Interpreter..."
        )
        agent = Agent(
            name="BasicAgent",
            system_prompt=system_prompt,
            tools=[gateway_client, code_tools.execute_python_securely],
            model=bedrock_model,
            session_manager=session_manager,
            trace_attributes={
                "user.id":    user_id,
                "user.org":   org_id if org_id else tenant_id,
                "user.role":  user_role,
                "session.id": session_id,
            },
        )
        print(
            "[AGENT] Agent created successfully with Gateway tools and Code Interpreter"
        )
        return agent

    except Exception as e:
        print(f"[AGENT ERROR] Error creating Gateway client: {e}")
        print(f"[AGENT ERROR] Exception type: {type(e).__name__}")
        print("[AGENT ERROR] Traceback:")
        traceback.print_exc()
        print(
            "[AGENT] Gateway connection failed - raising exception instead of fallback"
        )
        raise


@app.entrypoint
async def agent_stream(payload, context: RequestContext):
    """
    Main entrypoint for the agent using streaming with Gateway integration.

    This is the function that AgentCore Runtime calls when the agent receives a request.
    It extracts the user's query from the payload, securely obtains the user ID from
    the validated JWT token in the request context, extracts the user's role from
    Cognito groups in the JWT, creates an appropriately configured agent, and streams
    the response back.

    Role extraction (Phase 3 RBAC):
      - user_id  → from JWT 'sub' claim (tenantId for document scoping)
      - user_role → from JWT 'cognito:groups' claim via utils/role.py
        INTERNAL: full access, full citations
        EXTERNAL: restricted content, masked citations
    """
    user_query = payload.get("prompt")
    session_id = payload.get("runtimeSessionId")

    if not all([user_query, session_id]):
        yield {
            "status": "error",
            "error": "Missing required fields: prompt or runtimeSessionId",
        }
        return

    try:
        # Resolve principal — extracts user_id, org_id, role_class from JWT + membership table
        # org_id is the authoritative tenant identifier for all RAG retrieve calls
        try:
            principal = resolve_principal_from_context(context)
            user_id   = principal.user_id
            org_id    = principal.org_id
            user_role = principal.role_class
        except MembershipError as me:
            # User has no active org membership — cannot proceed
            print(f"[STREAM] Membership resolution failed: {me}")
            yield {"status": "error", "error": "Your account is not yet configured for access. Please complete registration or contact your administrator."}
            return
        except Exception:
            # Fall back to JWT-only extraction for backward compat during migration
            user_id   = extract_user_id_from_context(context)
            org_id    = user_id   # pre-Phase-4 fallback: user sub as tenant
            user_role = get_user_role_from_context(context)
            print(f"[STREAM][WARN] Principal resolution failed, using JWT fallback user={user_id}")

        print(
            f"[STREAM] Starting invocation: user={user_id} org={org_id} "
            f"role={user_role} session={session_id}"
        )
        print(f"[STREAM] Query: {user_query}")

        agent = create_basic_agent(user_id, session_id, user_role, org_id=org_id)

        # Use the agent's stream_async method for true token-level streaming
        async for event in agent.stream_async(user_query):
            yield json.loads(json.dumps(dict(event), default=str))

    except Exception as e:
        print(f"[STREAM ERROR] Error in agent_stream: {e}")
        traceback.print_exc()
        yield {"status": "error", "error": str(e)}


if __name__ == "__main__":
    app.run()
