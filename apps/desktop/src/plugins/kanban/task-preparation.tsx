import { Button, ErrorState, Input, Textarea } from '@hermes/plugin-sdk'
import { useState } from 'react'

import { authoriseTask, prepareTask } from './api'
import type { KanbanTaskFull } from './types'
import { errText, Section, useKanban } from './ui'

export function TaskPreparation({ task, onSaved }: { task: KanbanTaskFull; onSaved: () => Promise<unknown> }) {
  const k = useKanban()
  const saved = task.workspace_set

  const [rows, setRows] = useState(() => saved?.repositories.map(({ repository, path, base, branch }) => ({ repository, path, base, branch })) ?? [{
    repository: task.repository_identity?.replace(/^github.com\//, '') ?? '',
    path: task.repository_identity ? task.workspace_path?.split('/.worktrees/')[0] ?? '' : task.workspace_path ?? '',
    base: task.approved_base ?? '', branch: task.branch_name ?? ''
  }])

  const [manager, setManager] = useState(saved?.package_manager ?? '')
  const [commands, setCommands] = useState(saved?.test_commands.join('\n') ?? '')
  const [links, setLinks] = useState(saved?.links ?? [])
  const [authority, setAuthority] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const labels = { repository: k.repositoryIdentity, path: k.primaryCheckout, base: k.approvedBase, branch: k.taskBranch }

  const write = async (action: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)

    try {
      await action()
      await onSaved()
    } catch (cause) {
      setError(errText(cause))
    } finally {
      setBusy(false)
    }
  }

  const prepare = () => write(() => rows.length === 1 && !saved
    ? prepareTask(task.id, { repository: rows[0].repository, repository_path: rows[0].path, base: rows[0].base, branch: rows[0].branch })
    : prepareTask(task.id, { manifest: { repositories: rows, links, package_manager: manager,
        test_commands: commands.split('\n').filter(line => line.trim()) } }, true))

  return <Section label={k.prepareWorkspaces}>
    <p className="text-xs text-(--ui-text-tertiary)">{k.draftHelp}</p>
    <p className="text-xs">{k.executionAuthority}: {task.execution_authority || k.empty}</p>
    <p className="text-xs">{task.publication?.label || k.publicationNeedsAuthority}</p>
    {Object.entries(task.publication?.repositories ?? {}).map(([identity, scope]) => <p className="break-all text-xs" key={identity}>
      {identity}: {scope.authority || k.empty}, {scope.actions.join(', ') || k.empty}
    </p>)}
    {saved?.repositories.map(row => <p className="break-all font-mono text-xs" key={row.repository}>
      {row.repository}: {row.workspace_path}, {row.branch}, {row.base}
    </p>)}
    {task.status !== 'running' && task.status !== 'done' && task.status !== 'archived' && <div className="flex flex-col gap-3">
      {rows.map((row, index) => <div className="flex flex-col gap-2" key={index}>
        {(Object.keys(labels) as Array<keyof typeof labels>).map(key => <label className="flex flex-col gap-1 text-xs" key={key}>
          {labels[key]}
          <Input disabled={busy} onChange={event => setRows(rows.map((entry, i) => i === index ? { ...entry, [key]: event.target.value } : entry))} value={row[key]} />
        </label>)}
      </div>)}
      <Button disabled={busy || Boolean(saved)} onClick={() => setRows([...rows, { repository: '', path: '', base: '', branch: '' }])} variant="text">{k.addRepository}</Button>
      {rows.length > 1 && <>
        {links.map((link, index) => <div className="flex flex-col gap-2" key={index}>
          {(['consumer', 'dependency', 'package'] as const).map(key => <label className="flex flex-col gap-1 text-xs" key={key}>
            {{ consumer: k.linkConsumer, dependency: k.linkDependency, package: k.linkPackage }[key]}
            <Input onChange={event => setLinks(links.map((entry, i) => i === index ? { ...entry, [key]: event.target.value } : entry))} value={link[key]} />
          </label>)}
        </div>)}
        <Button disabled={busy || Boolean(saved)} onClick={() => setLinks([...links, { consumer: '', dependency: '', package: '' }])} variant="text">{k.addLink}</Button>
        <label className="flex flex-col gap-1 text-xs">{k.packageManagerVersion}<Input onChange={event => setManager(event.target.value)} value={manager} /></label>
        <label className="flex flex-col gap-1 text-xs">{k.integrationCommands}<Textarea onChange={event => setCommands(event.target.value)} value={commands} /></label>
      </>}
      <p className="text-xs">{k.requiredRepositoryReview}</p>
      <Button disabled={busy} onClick={() => void prepare()}>{k.prepareWorkspaces}</Button>
      <label className="flex flex-col gap-1 text-xs">{k.executionAuthority}<Input onChange={event => setAuthority(event.target.value)} value={authority} /></label>
      <Button disabled={busy || !authority.trim() || !task.body || !task.review_required} onClick={() => void write(() => authoriseTask(task.id, authority.trim()))}>{k.authoriseExecution}</Button>
    </div>}
    {error && <ErrorState title={error} />}
  </Section>
}
