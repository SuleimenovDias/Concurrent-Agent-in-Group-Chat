"""One minimal Responses request using the configured router. No retries or model comparison."""
import argparse
import asyncio

from config import Settings
from executor import OpenAIGateway, safe_error
from router import OpenAIRouter


async def main() -> bool:
    settings = Settings().model_copy(update={'max_retries': 0, 'router_max_output_tokens': 128})
    gateway = OpenAIGateway(settings)
    try:
        decision = await OpenAIRouter(gateway, settings).route({
            'sender': 'smoke-user', 'new_message': '@CastleBot Explain photosynthesis in one sentence.',
            'tasks': [], 'history': [],
        })
        if decision.action != 'new_task' or decision.target_task_ids:
            print('Smoke test did not produce the expected routing decision.')
            return False
        print(f'PASS: {settings.router_model}, Responses API, validated structured routing output.')
        print('One generation request; output capped at 128 tokens; retries disabled.')
        return True
    except Exception as exc:
        print('Smoke test failed:', safe_error(exc))
        print('No retry or full live scenario was run.')
        return False
    finally:
        await gateway.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Authorize one potentially billable request')
    if not parser.parse_args().live:
        parser.error('--live is required')
    raise SystemExit(0 if asyncio.run(main()) else 1)
