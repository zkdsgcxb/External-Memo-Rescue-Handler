"""Chinese terminal confirmation and JSON dispatch, before the mutation lock."""
import sys

import candidates
import discovery
import planning

NOTICE = ('候选核验可能读取选中介质，复用 plan 的完整准入且没有硬性总时限。'
          '确认后持锁重建计划，写候选后再次核验；不启用保护，单纯重启不会生效。')


def confirm(expected, actual, prompt, *, json_output):
    if expected is not None:
        candidates.expected_digest(expected)
        candidates.require(expected == actual, 'plan_changed')
        return True
    candidates.require(not json_output and sys.stdin.isatty(), 'confirmation_requires_expect_plan')
    try:
        return input(prompt + ' 输入“确认”继续，其余输入退出：').strip() == '确认'
    except (EOFError, KeyboardInterrupt):
        return False


def stage(device, expected, *, json_output=False):
    import manage
    if expected is not None:
        candidates.expected_digest(expected)
    candidates.require(device is not None or (not json_output and sys.stdin.isatty()), 'device_required')
    if device is None:
        observed = discovery.collect()
        print(discovery.format_text(observed))
        try:
            number = int(input('输入对象编号生成计划，直接回车退出：'))
            candidates.require(1 <= number <= len(observed['volumes']), 'invalid_selection')
            device = observed['volumes'][number - 1]['device']
        except (ValueError, EOFError, KeyboardInterrupt):
            return {'state': 'declined', 'persistent_writes': []}
    print(NOTICE, file=sys.stderr)
    plan = planning.build(device)
    if not json_output:
        print(planning.format_text(plan))
    if plan['status'] != 'ready':
        return {'state': 'blocked', 'plan': plan, 'persistent_writes': []}
    if not confirm(expected, plan['plan_digest'], '仅保存候选，尚未启用。', json_output=json_output):
        return {'state': 'declined', 'persistent_writes': []}
    return manage.stage_candidate(device, plan['plan_digest'])


def cancel(operation, expected, *, json_output=False):
    import manage
    store = candidates.Store()
    receipt, raw = store.load(operation)
    if not json_output:
        print('仅撤销候选 %s；将删除存在且摘要匹配的 plan.json / candidate.json，'
              '保留 manifest.json（如已写入）与 operation.json 小收据。当前保护不会改变。' % operation)
        print('计划摘要：' + receipt['plan_digest'])
    if not confirm(expected, receipt['plan_digest'], '确认撤销上述候选。', json_output=json_output):
        return {'state': 'declined', 'persistent_writes': []}
    return manage.cancel_candidate(operation, receipt['plan_digest'],
                                   {name: candidates.sha(value) for name, value in raw.items()})


def show(args):
    try:
        result = (stage(args.device, args.expect_plan, json_output=args.json) if args.command == 'stage'
                  else cancel(args.operation, args.expect_plan, json_output=args.json))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        result = {'state': 'failed', 'reason': str(error), 'enabled': False,
                  'instruction': '保留现场；读取 manager status 后审阅，不自动修补或递归删除。'}
    if args.json:
        print(candidates.encode(result).decode())
    elif result['state'] in ('prepared', 'cancelled'):
        print(('候选已准备，尚未启用；仅重启不会生效。' if result['state'] == 'prepared'
               else '候选已撤销；活动配置未改变。') + '\n操作 ID：' + result['id'])
        if result.get('preserved_needs_review'):
            print('另有候选残留保留待审阅：' + '、'.join(discovery.safe_text(item)
                                                   for item in result['preserved_needs_review']))
    else:
        print({'declined': '已退出，未写入候选。', 'blocked': '计划存在阻碍，未写入候选。',
               'failed': '操作未成功：' + result.get('reason', '')}[result['state']])
    if result['state'] in ('blocked', 'failed'):
        raise SystemExit(2)
