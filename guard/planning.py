"""Read-only candidate plans for one selected, existing data DM mapping.

Candidate persistence is separate; this module only reconstructs plans.
The admission adapter may read media and has no hard wall-clock deadline.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import subprocess
import sys

import discovery
import support
import diagnostics as doctor
from admin import data as admission
from admin.admission import digest
from admin.registry import INSTANCE_FIELDS, record_from_profile

IO_NOTICE = ('计划核验可能读取选中分区的文件系统与分区身份元数据。'
             '身份核验外部命令设置 3 秒尝试超时，末段准入使用 15 秒协作预算；'
             '下层阻塞、同步 ioctl 和进程回收没有硬性总时限。此命令只输出计划，不保存或启用配置。')
FLAGS = ('suspended', 'internal_suspend', 'deferred_remove', 'read_only', 'live_table', 'major', 'minor')
ADMINISTRATION_MANIFEST = str(Path(__file__).resolve().parent.parent / 'administration.json')
REASONS = {
    'combination_unvalidated': '候选准备前置资格或当前产物关联证据不足，禁止准备',
    'support_inputs_changed': '当前产物证据或资格内容在核验期间变化，必须重新规划',
    'support_context_mismatch': '组合产物与本次核验的内核或管理清单不一致',
    'discovery_incomplete': '发现快照变化或必要信息不完整',
    'object_not_resolved': '未定位唯一的既有约定 DM 映射；本切片不建立映射',
    'selection_changed': '所选映射与发现的 USB 分区关系不一致',
    'existing_registration': '已有登记或根盘控制器，本计划不重复登记或修改其身份',
    'root_provisioning_out_of_scope': '根盘启动集成不在本候选计划切片内',
    'context_unavailable': '挂载、swap、fstab 或设备实例输入不可完整读取',
    'map_unavailable': '无法读取选中 UUID 的当前 DM 表',
    'admission_failed': '现有完整准入未通过；没有生成候选登记',
    'admission_instance_changed': '准入结果与选定设备实例或内核不一致',
    'environment_changed': '准入后环境检查未通过',
    'map_changed': '选中 DM 表在核验期间变化或不符合准入结果',
    'inputs_changed': '发现、登记或挂载等关键输入发生变化，必须重新规划',
}


def selected_map(name, uuid):
    # Reuse discovery's loaded-module/existing-control-node gate.
    return discovery.query_mapper().snapshot_by_uuid(name, uuid)


def map_inputs(snapshot):
    """Exclude counters such as open_count/event_nr, keep the actual table."""
    return {'uuid': snapshot['uuid'], 'active': snapshot['active'], 'inactive': snapshot['inactive'],
            'flags': {key: snapshot['info'].get(key) for key in FLAGS}}


def context_inputs(metadata, backing, mapped, reader=None):
    """Additional inputs consumed by admission, not another admission policy."""
    files = {
        '/proc/1/mountinfo': 2 * 1024 * 1024,
        '/proc/swaps': 256 * 1024,
        '/proc/1/root/etc/fstab': 256 * 1024,
        '/sys/class/block/' + backing['name'] + '/start': 128,
        '/sys/class/block/' + backing['name'] + '/partition': 128,
        '/sys/class/block/' + backing['parent'] + '/queue/logical_block_size': 128,
    }
    hashes = {path: hashlib.sha256(metadata.text(path, limit).encode()).hexdigest()
              for path, limit in files.items()}
    # Manifest identity uses the actual trusted bytes, not Metadata.text's stripped
    # representation. It must agree with the independently bound static subject.
    hashes[ADMINISTRATION_MANIFEST] = hashlib.sha256(
        (reader or doctor.Reader()).read(ADMINISTRATION_MANIFEST, limit=1024 * 1024)).hexdigest()
    return {'files': hashes,
            'partition_holders': metadata.names('/sys/class/block/' + backing['name'] + '/holders'),
            'map_holders': metadata.names('/sys/class/block/' + mapped['name'] + '/holders')}


def bind_profile(profile, item, backing, inputs):
    """Bind the existing admission result to this plan's selected instance."""
    config = profile['guard']
    if (config['map_name'] != item['name'] or config['map_uuid'] != item['uuid'] or
            config['initial_node'] != '/dev/' + backing['name'] or
            config['initial_sys_path'] != backing['path'] or
            config['initial_diskseq'] != int(backing['diskseq']) or
            config['kernel_release'] != inputs['environment']['kernel_release']):
        raise ValueError('admission_instance_changed')
    return record_from_profile(profile)


def build(device, *, discover=discovery.collect, resolver=None, metadata=None,
          attest=admission.collect, map_reader=selected_map, environment_check=admission.check_environment,
          current_support=None, support_reader=None):
    if resolver is None:
        from manage import resolve_map
        resolver = resolve_map
    metadata = metadata or discovery.Metadata()
    blockers = set()
    support_before = support.sample(current=current_support, reader=support_reader)

    def attempt(code, function):
        try:
            return function()
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
            blockers.add(code)
            return None

    before = {}
    observed = discover(inputs=before)
    if not observed['snapshot_stable'] or observed['issues'] or not before.get('boot_id'):
        blockers.add('discovery_incomplete')
    resolved = attempt('object_not_resolved', lambda: resolver(device))
    item, root = resolved if resolved else (None, None)
    selected = mapped = backing = None
    if item:
        matches = [row for row in before['nodes'].values()
                   if row['dm_name'] == item['name'] and row['dm_uuid'] == item['uuid']]
        if len(matches) == 1 and len(matches[0]['slaves']) == 1:
            mapped = matches[0]
            backing = before['nodes'].get(mapped['slaves'][0])
        if backing and backing['usb'] and backing['partition']:
            matches = [row for row in observed['volumes'] if row['device'] == '/dev/' + backing['name']]
            selected = matches[0] if len(matches) == 1 else None
        if not selected:
            blockers.add('selection_changed')
        elif selected['groups'] or (root and item['uuid'] == root['guard']['map_uuid']):
            blockers.add('existing_registration')
        elif selected['role'] == 'system' or not item['name'].startswith('rr-data-'):
            blockers.add('root_provisioning_out_of_scope')
        else:
            # An unregistered map necessarily appears as unmanaged to discover.
            # Its acceptability belongs to the complete existing admission below.
            blockers.update(row['code'] for row in selected['checks']
                            if row['result'] == 'fail' and row['code'] != 'unmanaged_upper_layers')
            blockers.update(row['code'] for row in observed['environment']['checks']
                            if row['result'] == 'unknown')

    context_before = context_after = table_before = table_after = None
    record = profile = None
    media = 'not_run'
    environment_result = 'not_checked'
    if not blockers:
        context_before = attempt('context_unavailable', lambda: context_inputs(metadata, backing, mapped, support_reader))
        table_before = attempt('map_unavailable', lambda: map_inputs(map_reader(item['name'], item['uuid'])))
        if context_before is not None and table_before is not None:
            media = 'attempted'
            profile = attempt('admission_failed', lambda: attest(item['name'], '/dev/' + backing['name']))
            if profile is not None:
                record = attempt('admission_instance_changed', lambda: bind_profile(profile, item, backing, before))
                if record is not None:
                    media = 'admission_passed'
                    check = attempt('environment_changed', lambda: (environment_check(profile['guard']), True)[1])
                    environment_result = 'passed' if check else 'failed'
            context_after = attempt('context_unavailable', lambda: context_inputs(metadata, backing, mapped, support_reader))
            snapshot = attempt('map_unavailable', lambda: map_reader(item['name'], item['uuid']))
            if snapshot is not None:
                table_after = attempt('map_unavailable', lambda: map_inputs(snapshot))
                if record is not None and attempt('map_changed', lambda: discovery.map_topology(
                        profile['guard'], mapped, snapshot, before['nodes'])) != 'pass':
                    blockers.add('map_changed')
            if table_before != table_after:
                blockers.add('map_changed')
            if context_before != context_after:
                blockers.add('inputs_changed')

    after = {}
    latest = discover(inputs=after)
    if not latest['snapshot_stable'] or latest['issues'] or digest(before) != digest(after):
        blockers.add('inputs_changed')

    support_after = support.sample(current=current_support, reader=support_reader)
    for sample, inputs, context in ((support_before, before, context_before), (support_after, after, context_after)):
        subject = sample['subject']
        if subject is not None and (subject['kernel']['release'] != inputs.get('environment', {}).get('kernel_release')
                or (context is not None and subject['administration']['manifest_sha256'] !=
                    context['files'][ADMINISTRATION_MANIFEST])):
            blockers.add('support_context_mismatch')
    policy = support.evaluate(support_before, support_after, record['identity']['fs_type'] if record else None)
    if support_before != support_after:
        blockers.add('support_inputs_changed')
    if policy['combination']['state'] != support.ELIGIBLE:
        blockers.add('combination_unvalidated')

    effects = {'action': 'save_candidate_only' if record else 'review_only',
               'destination_class': '/var/lib/ram-rescue-plans/operations/<generated-id>' if record else None,
               'candidate_files': ['plan.json', 'candidate.json', 'manifest.json', 'operation.json'] if record else [],
               'activation': 'none', 'reboot_activates': False,
               'candidate_record': record}
    confirmation = {
        'schema': 1, 'support_policy': policy,
        'selected_object': {'map_name': item['name'], 'map_uuid': item['uuid']} if item else {'unresolved': device},
        'scope': {'members': selected['members'], 'mounts': selected['mounts'],
                  'role': selected['role'], 'physical_disk_coverage': 'selected_volume_only'} if selected else None,
        'effects': effects,
        'inputs': {'discovery_before_sha256': digest(before), 'discovery_after_sha256': digest(after),
                   'support_before_sha256': digest(support_before), 'support_after_sha256': digest(support_after),
                   'configuration': before.get('configuration'), 'boot_id': before.get('boot_id'),
                   'kernel_release': before.get('environment', {}).get('kernel_release'),
                   'instance': {key: profile['guard'].get(key) for key in sorted(INSTANCE_FIELDS)} if profile else None,
                   'context_before': context_before, 'context_after': context_after,
                   'map_before': table_before, 'map_after': table_after,
                   'environment_recheck': environment_result},
        'blockers': sorted(blockers),
    }
    result = {'schema': 1, 'operation': 'plan', 'status': 'blocked' if blockers else 'ready',
              'plan_digest': digest(confirmation), 'confirmation': confirmation,
              'observation': {'selected': deepcopy(selected), 'snapshot_id': observed['snapshot_id'],
                              'captured_at_unix': observed['captured_at_unix']},
              'io': {'media': media, 'successful_admission_blkid_calls': [6, 7],
                     'identity_command_timeout_seconds': 3, 'mount_resolution_timeout_seconds': 45,
                     'systemd_query_timeout_seconds': 4, 'final_admission_budget_seconds': 15,
                     'hard_total_deadline': False},
              'current_effects': {'persistent_writes': [], 'activation': 'none'},
              'report_scope': 'local_not_redacted'}
    if len(doctor.encode(result)) > doctor.EXPORT_LIMIT:
        raise doctor.InputError('plan_output_limit')
    return result


def format_text(plan):
    confirmed = plan['confirmation']
    selected = plan['observation']['selected']
    lines = ['USB 存储保护 · 只读候选计划', IO_NOTICE]
    if selected:
        lines.extend(['选中对象：' + discovery.safe_text(selected['device']) + ' · ' + selected['model'],
                      '关联卷：' + '、'.join(confirmed['scope']['members']),
                      '挂载位置：' + ('、'.join(confirmed['scope']['mounts']) or '未观察到'),
                      '保护范围仅限上述卷，不覆盖同盘其他裸分区或 EFI。'])
    lines.append('介质核验：' + {'not_run': '未执行', 'attempted': '已尝试，未完成通过核验',
                                 'admission_passed': '现有完整准入通过（组合验收与计划阻碍另列）'}[plan['io']['media']])
    if confirmed['effects']['candidate_record']:
        lines.append('拟确认效果：仅在独立候选目录保存登记与操作记录；不启用，单纯重启不会生效。')
    else:
        lines.append('没有可准备的候选登记。')
    lines.append('组合资格仅涉及候选准备前置条件，不代表启用、根盘保护、恢复或完整发布验收。')
    for reason in confirmed['support_policy']['combination']['reasons']:
        lines.append('[资格] ' + reason)
    for code in confirmed['blockers']:
        lines.append('[阻碍] ' + REASONS.get(code, '未满足或未确认：' + discovery.REASONS.get(code, code)))
    lines.extend(['计划摘要：' + plan['plan_digest'],
                  '当前仅输出计划；stage 仅保存候选，不能据此确认启用。',
                  '这是本地未脱敏计划，含设备身份与挂载信息。'])
    return '\n'.join(lines)


def show(device, *, json_output=False):
    print(IO_NOTICE, file=sys.stderr)
    result = build(device)
    print(doctor.encode(result).decode() if json_output else format_text(result))
