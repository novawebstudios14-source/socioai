"""Consult/claim/complete confirmed commands using the owner's private credential file."""
import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE_URL = 'https://nova-phone-agent-llfy.onrender.com'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['list', 'get', 'claim', 'complete'])
    p.add_argument('--credentials-file', required=True)
    p.add_argument('--command-id')
    p.add_argument('--run-id')
    p.add_argument('--result')
    p.add_argument('--status', choices=['completed', 'failed'], default='completed')
    args = p.parse_args()
    key = json.loads(Path(args.credentials_file).read_text())['control_key']
    body = None
    if args.action != 'list':
        if not args.command_id:
            p.error('--command-id required')
        body = {'action': args.action, 'command_id': args.command_id}
        if args.action in ('claim', 'complete'):
            if not args.run_id:
                p.error('--run-id required')
            body['run_id'] = args.run_id
        if args.action == 'complete':
            if not args.result:
                p.error('--result required')
            body.update(status=args.status, result=args.result)
    req = Request(BASE_URL+'/control/voice-commands', data=json.dumps(body).encode() if body else None,
                  headers={'Authorization':'Bearer '+key, 'Content-Type':'application/json'})
    try:
        with urlopen(req, timeout=45) as response:
            print(json.dumps(json.load(response), ensure_ascii=False))
    except HTTPError as error:
        print(json.dumps({'error':'server_rejected_request', 'status':error.code}))
        raise SystemExit(1)
    except (URLError, TimeoutError):
        print(json.dumps({'error':'connection_unconfirmed'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
