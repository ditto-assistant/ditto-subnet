const els = {
  workspace: document.querySelector('.workspace'),
  form: document.querySelector('#dataset-form'),
  benchVersion: document.querySelector('#bench-version'),
  runSize: document.querySelector('#run-size'),
  seed: document.querySelector('#seed'),
  randomSeed: document.querySelector('#random-seed'),
  summary: document.querySelector('#dataset-summary'),
  casesTab: document.querySelector('#cases-tab'),
  memoriesTab: document.querySelector('#memories-tab'),
  search: document.querySelector('#search'),
  caseFilters: document.querySelector('#case-filters'),
  memoryFilters: document.querySelector('#memory-filters'),
  memorySort: document.querySelector('#memory-sort'),
  axisFilter: document.querySelector('#axis-filter'),
  typeFilter: document.querySelector('#type-filter'),
  flaggedOnly: document.querySelector('#flagged-only'),
  resultCount: document.querySelector('#result-count'),
  itemList: document.querySelector('#item-list'),
  inspector: document.querySelector('#inspector'),
  reviewPanel: document.querySelector('#review-panel'),
  evidenceCaption: document.querySelector('#evidence-caption'),
  evidenceList: document.querySelector('#evidence-list'),
  timelineButton: document.querySelector('#timeline-button'),
  flagForm: document.querySelector('#flag-form'),
  flagReason: document.querySelector('#flag-reason'),
  flagNote: document.querySelector('#flag-note'),
  flagState: document.querySelector('#flag-state'),
  removeFlag: document.querySelector('#remove-flag'),
  flagTotal: document.querySelector('#flag-total'),
  copyLink: document.querySelector('#copy-link'),
  downloadArtifact: document.querySelector('#download-artifact'),
  exportFlags: document.querySelector('#export-flags'),
  toast: document.querySelector('#toast'),
};

const FLAG_STORAGE_KEY = 'dittobench.datasetviewer.flags.v1';
const state = {
  config: null,
  response: null,
  artifact: null,
  annotations: new Map(),
  pairs: [],
  pairCandidatesByID: new Map(),
  cases: [],
  items: [],
  filteredItems: [],
  selectedKey: '',
  mode: 'cases',
  memorySort: 'time',
  flags: loadFlags(),
  repeatedOpeners: new Map(),
  repeatedResponses: new Map(),
	renderLimit: 120,
};

init().catch((error) => showFatal(error.message));

async function init() {
  state.config = await fetchJSON('/api/config');
  const query = new URLSearchParams(location.search);
  for (const version of state.config.supported_versions) {
    const option = document.createElement('option');
    option.value = String(version);
    option.textContent = `V${version}`;
    els.benchVersion.append(option);
  }
  els.benchVersion.value = query.get('bench_version') || String(state.config.default_bench_version);
  els.runSize.value = query.get('run_size') || state.config.default_run_size;
  els.seed.value = query.get('seed') || String(state.config.default_seed);
  state.mode = query.get('view') === 'memories' ? 'memories' : 'cases';
  state.memorySort = query.get('sort') === 'category' ? 'category' : 'time';
  els.memorySort.value = state.memorySort;
  state.selectedKey = query.get('item') || '';
  bindEvents();
  await loadDataset();
}

function bindEvents() {
  els.form.addEventListener('submit', async (event) => {
    event.preventDefault();
    state.selectedKey = '';
    await loadDataset();
  });
  els.randomSeed.addEventListener('click', async () => {
    const data = await fetchJSON('/api/fresh-seed');
    els.seed.value = String(data.seed);
    state.selectedKey = '';
    await loadDataset();
  });
  els.casesTab.addEventListener('click', () => setMode('cases'));
  els.memoriesTab.addEventListener('click', () => setMode('memories'));
  els.search.addEventListener('input', resetAndRenderList);
  els.axisFilter.addEventListener('change', resetAndRenderList);
  els.typeFilter.addEventListener('change', resetAndRenderList);
  els.memorySort.addEventListener('change', () => {
    state.memorySort = els.memorySort.value;
    resetAndRenderList();
    updateURL();
  });
  els.flaggedOnly.addEventListener('change', resetAndRenderList);
  els.flagForm.addEventListener('submit', saveCurrentFlag);
  els.removeFlag.addEventListener('click', removeCurrentFlag);
  els.copyLink.addEventListener('click', copyDeepLink);
  els.downloadArtifact.addEventListener('click', downloadArtifact);
  els.exportFlags.addEventListener('click', exportCurrentFlags);
  els.timelineButton.addEventListener('click', () => {
    setMode('memories');
    state.memorySort = 'time';
    els.memorySort.value = 'time';
    els.search.value = '';
    renderList();
  });
  document.addEventListener('keydown', handleKeyboard);
}

async function loadDataset() {
  setLoading(true);
  const params = new URLSearchParams({
    seed: els.seed.value.trim(),
    run_size: els.runSize.value,
    bench_version: els.benchVersion.value,
  });
  try {
    state.response = await fetchJSON(`/api/dataset?${params}`);
    state.artifact = state.response.review.artifact;
    state.annotations = new Map(
      (state.response.review.memory_annotations || []).map((annotation) => [annotation.case_id, annotation]),
    );
	state.renderLimit = 120;
    indexDataset();
    populateTypeFilter();
    renderSummary();
    renderTabs();
    renderList();
    updateURL();
  } catch (error) {
    showFatal(error.message);
  } finally {
    setLoading(false);
  }
}

function indexDataset() {
  state.pairs = [];
  state.pairCandidatesByID = new Map();
  const recordSignatures = new Set();
  for (const [waveIndex, wave] of (state.artifact.memory_waves || []).entries()) {
    const subjectByID = new Map((wave.subjects || []).map((subject) => [subject.id, subject]));
    const subjectsByPair = new Map();
    for (const link of wave.links || []) {
      if (!subjectsByPair.has(link.pair_id)) subjectsByPair.set(link.pair_id, []);
      const subject = subjectByID.get(link.subject_id);
      if (subject) subjectsByPair.get(link.pair_id).push(subject);
    }
    for (const [pairIndex, pair] of (wave.pairs || []).entries()) {
      const indexed = {
        ...pair,
        dataset_index: state.pairs.length,
        wave: wave.wave ?? waveIndex,
        user_id: wave.user_id || 'miner',
        subjects: subjectsByPair.get(pair.pair_id) || [],
		source: 'memory wave',
		review_key: `wave:${wave.wave ?? waveIndex}:${wave.user_id || 'miner'}:${pair.pair_id}:${pairIndex}`,
      };
      state.pairs.push(indexed);
	  addPairCandidate(indexed);
	  recordSignatures.add(memoryRecordSignature(indexed));
    }
  }
	for (const toolCase of state.artifact.tool_cases || []) {
		for (const [pairIndex, pair] of (toolCase.prerequisite_pairs || []).entries()) {
			const signature = memoryRecordSignature({...pair, user_id: 'miner'});
			if (recordSignatures.has(signature)) continue;
			const indexed = {
				...pair,
				dataset_index: state.pairs.length,
				wave: 'prerequisite',
				user_id: 'miner',
				subjects: [],
				source: 'tool prerequisite',
				review_key: `prerequisite:${toolCase.id}:${pair.pair_id}:${pairIndex}`,
			};
			state.pairs.push(indexed);
			addPairCandidate(indexed);
			recordSignatures.add(signature);
		}
	}
  state.cases = [
    ...(state.artifact.tool_cases || []).map((data) => ({
      key: `tool:${data.id}`, kind: 'tool', type: data.category || 'uncategorized', data,
      label: data.category || 'tool case', prompt: data.prompt || '',
    })),
    ...(state.artifact.memory_cases || []).map((data) => ({
      key: `memory:${data.id}`, kind: 'memory', type: data.question_type || 'uncategorized', data,
      label: data.question_type || 'memory case', prompt: data.question || '',
    })),
  ];
  state.repeatedOpeners = frequencyMap(state.pairs.flatMap((pair) => paragraphOpeners(pair.prompt)));
  state.repeatedResponses = frequencyMap(state.pairs.map((pair) => normalizeSpace(pair.response)));
  state.items = state.mode === 'cases' ? state.cases : memoryItems();
}

function memoryItems() {
  return state.pairs.map((data) => ({
	key: `pair:${data.review_key}`,
    kind: 'pair',
    type: data.session_id || 'unsessioned',
    category: memoryCategory(data),
    data,
    label: data.session_id || 'memory record',
    prompt: `${data.prompt || ''} ${data.response || ''}`,
  }));
}

function addPairCandidate(pair) {
	if (!state.pairCandidatesByID.has(pair.pair_id)) state.pairCandidatesByID.set(pair.pair_id, []);
	state.pairCandidatesByID.get(pair.pair_id).push(pair);
}

function memoryRecordSignature(pair) {
	return [pair.user_id || 'miner', pair.pair_id, pair.session_id, pair.timestamp, pair.prompt, pair.response].join('\u0000');
}

function setMode(mode) {
  if (state.mode === mode) return;
  state.mode = mode;
  state.selectedKey = '';
	state.renderLimit = 120;
  state.items = mode === 'cases' ? state.cases : memoryItems();
  els.search.value = '';
  els.axisFilter.value = 'all';
  els.typeFilter.value = 'all';
  renderTabs();
  populateTypeFilter();
  renderList();
  updateURL();
}

function renderTabs() {
  const cases = state.mode === 'cases';
  els.casesTab.classList.toggle('active', cases);
  els.memoriesTab.classList.toggle('active', !cases);
  els.casesTab.setAttribute('aria-selected', String(cases));
  els.memoriesTab.setAttribute('aria-selected', String(!cases));
  els.caseFilters.hidden = !cases;
  els.memoryFilters.hidden = cases;
}

function populateTypeFilter() {
  const previous = els.typeFilter.value;
  const types = [...new Set(state.items.map((item) => item.type))].sort();
  els.typeFilter.innerHTML = '<option value="all">All types</option>';
  for (const type of types) {
    const option = document.createElement('option');
    option.value = type;
    option.textContent = type;
    els.typeFilter.append(option);
  }
  els.typeFilter.value = types.includes(previous) ? previous : 'all';
}

function renderSummary() {
  const summary = state.response.summary;
  const sha = state.response.dataset_sha256;
  els.summary.innerHTML = [
    `<strong>Bench V${state.artifact.bench_version}</strong>`,
    escapeHTML(state.response.run_size),
    `seed <code>${escapeHTML(String(state.artifact.seed))}</code>`,
    `${summary.tool_cases + summary.memory_cases} cases (${summary.tool_cases} tool + ${summary.memory_cases} memory)`,
	`${summary.memory_records} memory entries (${summary.prerequisite_records} tool-prerequisite${summary.repeated_pair_identities ? ` · ${summary.repeated_pair_identities} updated identities` : ''}) / ${summary.memory_waves} waves`,
    `SHA <code title="${escapeHTML(sha)}">${escapeHTML(shortID(sha, 12))}</code>`,
  ].join(' · ');
  updateFlagCount();
}

function renderList() {
  if (!state.artifact) return;
  const query = normalizeSpace(els.search.value).toLowerCase();
  const axis = els.axisFilter.value;
  const type = els.typeFilter.value;
  const datasetKey = currentDatasetKey();
  state.filteredItems = state.items.filter((item) => {
    if (state.mode === 'cases' && axis !== 'all' && item.kind !== axis) return false;
    if (type !== 'all' && item.type !== type) return false;
    if (els.flaggedOnly.checked && !flagFor(datasetKey, item.key)) return false;
    if (!query) return true;
    const data = item.data;
    const searchable = [item.key, item.type, item.label, item.prompt, data.expected_answer,
      data.user_id, data.session_id, ...(data.expected_tools || []).map((tool) => tool.name)].join(' ').toLowerCase();
    return searchable.includes(query);
  });
  if (state.mode === 'memories') state.filteredItems.sort(compareMemoryItems);

  els.resultCount.textContent = `${state.filteredItems.length} of ${state.items.length} ${state.mode === 'cases' ? 'cases' : 'memories'}`;
  if (!state.filteredItems.some((item) => item.key === state.selectedKey)) {
    state.selectedKey = state.filteredItems[0]?.key || '';
  }
	const selectedIndex = state.filteredItems.findIndex((item) => item.key === state.selectedKey);
	if (selectedIndex >= state.renderLimit) state.renderLimit = selectedIndex + 1;
	const visibleItems = state.filteredItems.slice(0, state.renderLimit);
	els.resultCount.textContent = state.filteredItems.length > visibleItems.length
		? `${state.filteredItems.length} matches · showing ${visibleItems.length}`
		: `${state.filteredItems.length} of ${state.items.length} ${state.mode === 'cases' ? 'cases' : 'memories'}`;
  els.itemList.innerHTML = itemListHTML(visibleItems, datasetKey) ||
    '<div class="empty-state"><strong>No matches.</strong><span>Clear a filter or search term.</span></div>';
	if (visibleItems.length < state.filteredItems.length) {
		els.itemList.insertAdjacentHTML('beforeend', `<button id="show-more" class="show-more" type="button">Show ${Math.min(120, state.filteredItems.length - visibleItems.length)} more</button>`);
		els.itemList.querySelector('#show-more').addEventListener('click', () => {
			state.renderLimit += 120;
			renderList();
		});
	}
  for (const button of els.itemList.querySelectorAll('.item-row')) {
    button.addEventListener('click', () => selectItem(button.dataset.key));
  }
  renderSelection();
}

function itemListHTML(items, datasetKey) {
  if (state.mode !== 'memories' || state.memorySort !== 'category') {
    return items.map((item) => itemRowHTML(item, datasetKey)).join('');
  }
  const categoryCounts = frequencyMap(state.filteredItems.map((item) => item.category));
  let currentCategory = '';
  let html = '';
  for (const item of items) {
    if (item.category !== currentCategory) {
      if (currentCategory) html += '</div>';
      currentCategory = item.category;
      html += `<div class="item-group" role="group" aria-label="${escapeHTML(currentCategory)}">
        <div class="item-group-heading" aria-hidden="true"><span>${escapeHTML(currentCategory)}</span><small>${categoryCounts.get(currentCategory) || 0}</small></div>`;
    }
    html += itemRowHTML(item, datasetKey);
  }
  if (currentCategory) html += '</div>';
  return html;
}

function resetAndRenderList() {
	state.renderLimit = 120;
	renderList();
}

function itemRowHTML(item, datasetKey) {
  const data = item.data;
  const flagged = Boolean(flagFor(datasetKey, item.key));
  const meta = item.kind === 'pair'
    ? [formatTimestamp(data.timestamp), item.category, data.user_id, `${(data.prompt || '').length} chars`]
    : [item.kind, shortID(data.id), item.kind === 'memory' ? `wave ${data.run_after_wave || 0}` : `${(data.expected_tools || []).length} expected`];
  return `<button class="item-row ${item.key === state.selectedKey ? 'selected' : ''}" type="button" role="option" aria-selected="${item.key === state.selectedKey}" data-key="${escapeHTML(item.key)}">
    <span class="item-row-top">
      <span class="badge ${item.kind === 'pair' ? 'memory' : item.kind}">${escapeHTML(item.kind === 'pair' ? 'record' : item.kind)}</span>
      <span class="item-row-title">${escapeHTML(item.label)}</span>
      ${flagged ? '<span class="flag-dot" title="Flagged"></span>' : ''}
    </span>
    <span class="item-row-preview">${escapeHTML(item.kind === 'pair' ? data.prompt : item.prompt)}</span>
    <span class="item-row-meta">${meta.map(escapeHTML).join('<span>·</span>')}</span>
  </button>`;
}

function selectItem(key) {
  state.selectedKey = key;
  for (const row of els.itemList.querySelectorAll('.item-row')) {
    const selected = row.dataset.key === key;
    row.classList.toggle('selected', selected);
    row.setAttribute('aria-selected', String(selected));
  }
  renderSelection();
  updateURL();
}

function renderSelection() {
  const item = state.items.find((candidate) => candidate.key === state.selectedKey);
  if (!item) {
    els.inspector.innerHTML = '<div class="empty-state"><strong>Select a case or memory.</strong><span>Use ↑ and ↓ to move through the list.</span></div>';
    els.evidenceList.innerHTML = '';
    els.flagForm.hidden = true;
    return;
  }
  els.flagForm.hidden = false;
  if (item.kind === 'tool') renderToolCase(item);
  else if (item.kind === 'memory') renderMemoryCase(item);
  else renderMemoryRecord(item);
  renderFlagForm(item);
}

function renderToolCase(item) {
  const c = item.data;
  const expected = c.expected_tools || [];
  const prerequisites = c.prerequisite_pairs || [];
	const rankedPrerequisites = rankToolEvidence(c, prerequisites, 12);
  const operations = expected.map((tool, index) => `<div class="trajectory-step"><span>${index + 1}</span><div><strong>${escapeHTML(tool.name)}</strong><small>${formatArgs(tool.required_args)}</small></div></div>`).join('');
  els.inspector.innerHTML = `${detailHeader('tool', c.category, c.id)}
    ${humanSection('Agent-visible request', c.prompt, `${expected.length} expected capabilities`)}
    <section class="section">
      <div class="section-heading"><h3>Reviewer-only outcome contract</h3><span>never sent as an answer key</span></div>
      <div class="trajectory">${operations || '<p class="human-text">No expected tool calls.</p>'}</div>
    </section>
    <section class="section">
      <div class="section-heading"><h3>Trajectory policy</h3></div>
      <dl class="definition-list">
        <dt>Expected behavior</dt><dd>${escapeHTML(c.expected_behavior || 'Not specified')}</dd>
        <dt>Expected envelope</dt><dd>${escapeHTML(String(c.max_tool_calls || 0))} calls${c.allow_extra_tools ? '; creative extra calls allowed' : ''}</dd>
        <dt>Ordering</dt><dd>${c.unordered ? 'Independent / unordered' : c.fuzzy_trajectory ? 'Fuzzy outcome-driven trajectory' : 'Ordered contract'}</dd>
        <dt>Seeded context</dt><dd>${prerequisites.length} prerequisite memories</dd>
      </dl>
    </section>
    ${qualitySection(toolSignals(c))}`;
  els.evidenceCaption.textContent = prerequisites.length
		? `Showing ${rankedPrerequisites.length} likely-relevant records from ${prerequisites.length} seed-bound prerequisites. Ranking is reviewer-only lexical guidance.`
		: 'This tool task has no seeded prerequisite memory.';
  els.timelineButton.hidden = prerequisites.length === 0;
  renderEvidenceRecords(rankedPrerequisites, [], rankedPrerequisites.map((pair) => pair.pair_id));
}

function renderMemoryCase(item) {
  const c = item.data;
  const annotation = state.annotations.get(c.id);
  const terms = answerTerms(c);
  const evidenceIDs = annotation?.required_pair_ids || [];
  const records = evidenceIDs.map((id) => resolveEvidencePair(c, id)).filter(Boolean);
  const literalRecords = evidenceIDs.length ? [] : literalEvidence(c);
  const facts = annotation?.facts || [];
  const constraints = annotation?.constraints || [];
  const operations = annotation?.operations || [];
  els.inspector.innerHTML = `${detailHeader('memory', c.question_type, c.id)}
    ${humanSection('Agent-visible question', c.question, `user ${c.user_id || 'miner'} · after wave ${c.run_after_wave || 0}`)}
    <section class="section">
      <div class="section-heading"><h3>Oracle</h3><span>reviewer only · never sent to harness</span></div>
      <div class="oracle-block">
        <div class="oracle-label">CANONICAL ${escapeHTML((c.answer_kind || 'value').toUpperCase())}</div>
        <div class="oracle-answer">${escapeHTML(c.expected_answer || '(behavioral answer)')}</div>
        <div class="oracle-meta">
          ${c.accept_any?.length ? `<span class="badge">${c.accept_any.length} accepted forms</span>` : ''}
          ${c.answer_items?.length ? `<span class="badge">${c.answer_items.length} required items</span>` : ''}
          ${c.distractor_answers?.length ? `<span class="badge warn">${c.distractor_answers.length} distractors</span>` : ''}
          ${c.forbidden_answer ? '<span class="badge warn">forbidden cross-user value</span>' : ''}
          ${c.twin_group ? '<span class="badge">metamorphic twin</span>' : ''}
        </div>
      </div>
    </section>
    ${operations.length ? `<section class="section"><div class="section-heading"><h3>Intended reasoning burden</h3><span>${evidenceIDs.length} planted records</span></div>
      <div class="trajectory">${operations.map((operation, index) => `<div class="trajectory-step"><span>${index + 1}</span><div><strong>${escapeHTML(operation)}</strong><small>${escapeHTML(facts[index] || '')}</small></div></div>`).join('')}</div>
      ${constraints.length ? `<div class="section-heading compact"><h3>Disambiguation constraints</h3></div><div class="token-list">${constraints.map((value) => `<span class="token">${escapeHTML(value)}</span>`).join('')}</div>` : ''}
    </section>` : ''}
    ${structuredCitationSection(annotation)}
    <section class="section">
      <div class="section-heading"><h3>Negative grading guards</h3></div>
      <dl class="definition-list">
        <dt>Accepted forms</dt><dd>${formatValues(c.accept_any)}</dd>
        <dt>List items</dt><dd>${formatValues(c.answer_items)}</dd>
        <dt>Distractors</dt><dd>${formatValues(c.distractor_answers)}</dd>
        <dt>Forbidden</dt><dd>${escapeHTML(c.forbidden_answer || 'None')}</dd>
        <dt>Dump guard</dt><dd>${c.dump_guard?.length ? `${c.dump_guard.length} protected values` : 'None'}</dd>
        <dt>Bait tool</dt><dd>${escapeHTML(c.bait_tool || 'None')}</dd>
      </dl>
    </section>
    ${qualitySection(memoryCaseSignals(c, records.length ? records : literalRecords, annotation))}`;
  const evidence = records.length ? records : literalRecords;
  els.evidenceCaption.textContent = records.length
    ? `${records.length} exact records declared by the answerability oracle.`
    : literalRecords.length
      ? `${literalRecords.length} literal answer hit${literalRecords.length === 1 ? '' : 's'}; this legacy case has no explicit evidence plan.`
      : 'No literal answer hit. Inspect the timeline for computed, behavioral, or negative evidence.';
  els.timelineButton.hidden = false;
  renderEvidenceRecords(evidence, terms, evidenceIDs);
}

function structuredCitationSection(annotation) {
  const storyCitations = (annotation?.citations || []).filter((citation) => citation.story);
  if (!storyCitations.length) return '';
  return `<section class="section">
    <div class="section-heading"><h3>Structured story citations</h3><span>reviewer only · generated before prose</span></div>
    <div class="story-citations">${storyCitations.map((citation) => {
      const story = citation.story;
      const characters = (story.characters || []).map((character) => `${character.name} — ${character.role}${character.relationship ? ` (${character.relationship})` : ''}`);
      const resolutions = new Map((story.resolutions || []).map((resolution) => [resolution.problem_key, resolution]));
      return `<article class="story-citation">
        <div class="evidence-meta"><span>${escapeHTML(story.kind)} · ${escapeHTML(story.domain)}</span><span>${escapeHTML(shortID(citation.pair_id, 12))}</span></div>
        <h4>${escapeHTML(story.title)}</h4>
        <dl class="definition-list">
          <dt>Characters</dt><dd>${characters.map(escapeHTML).join('<br>')}</dd>
          <dt>Problems</dt><dd>${(story.problems || []).map((problem) => {
            const resolution = resolutions.get(problem.key);
            const resolved = resolution ? ` → ${resolution.action} → ${resolution.outcome}` : '';
            return `${escapeHTML(problem.description)} <small>(${escapeHTML(problem.raised_in)})</small>${escapeHTML(resolved)}`;
          }).join('<br>')}</dd>
          <dt>Themes</dt><dd>${(story.themes || []).map(escapeHTML).join(' · ')}</dd>
          <dt>Fact placement</dt><dd>${(story.fact_placements || []).map((fact) => `${escapeHTML(fact.key)} @ ${escapeHTML(fact.phase)}:${fact.after_event}`).join(' · ')}</dd>
        </dl>
      </article>`;
    }).join('')}</div>
  </section>`;
}

function renderMemoryRecord(item) {
  const pair = item.data;
  const linkedCases = [...state.annotations.values()].filter((annotation) => {
	const memoryCase = state.cases.find((candidate) => candidate.kind === 'memory' && candidate.data.id === annotation.case_id)?.data;
	return memoryCase && annotation.required_pair_ids.some((id) => resolveEvidencePair(memoryCase, id)?.review_key === pair.review_key);
  });
  const subjects = pair.subjects || [];
  els.inspector.innerHTML = `${detailHeader('memory', pair.session_id || 'memory record', pair.pair_id)}
    ${humanSection('User message', pair.prompt, `${pair.timestamp} · wave ${pair.wave} · user ${pair.user_id}`)}
    ${humanSection('Agent response', pair.response, `${pair.response.length} characters`, 'agent-response')}
    <section class="section">
      <div class="section-heading"><h3>Record context</h3></div>
      <dl class="definition-list">
        <dt>Session</dt><dd><code>${escapeHTML(pair.session_id || '—')}</code></dd>
		<dt>Ingestion</dt><dd>${escapeHTML(pair.source || 'memory wave')}</dd>
        <dt>Subjects</dt><dd>${subjects.length ? subjects.map((subject) => escapeHTML(subject.subject_text)).join(', ') : 'Raw pair; no prepared subject'}</dd>
        <dt>Required by</dt><dd>${linkedCases.length ? `${linkedCases.length} V8 world case${linkedCases.length === 1 ? '' : 's'}` : 'No explicit V8 world annotation'}</dd>
        <dt>Length</dt><dd>${pair.prompt.length.toLocaleString()} user characters · ${pair.response.length.toLocaleString()} agent characters</dd>
      </dl>
    </section>
    ${linkedCases.length ? `<section class="section"><div class="section-heading"><h3>Questions that require this record</h3></div><div class="token-list">${linkedCases.map((annotation) => `<button class="text-button case-jump" data-case="${escapeHTML(annotation.case_id)}" type="button">${escapeHTML(shortID(annotation.case_id, 12))}</button>`).join('')}</div></section>` : ''}
    ${qualitySection(memoryRecordSignals(pair))}`;
  for (const button of els.inspector.querySelectorAll('.case-jump')) {
    button.addEventListener('click', () => {
      setMode('cases');
      selectItem(`memory:${button.dataset.case}`);
    });
  }
  els.evidenceCaption.textContent = 'The selected memory is the evidence record.';
  els.timelineButton.hidden = true;
  renderEvidenceRecords([pair], [], [pair.pair_id]);
}

function resolveEvidencePair(memoryCase, pairID) {
	const candidates = state.pairCandidatesByID.get(pairID) || [];
	const userID = memoryCase.user_id || 'miner';
	const visible = candidates.filter((pair) => pair.user_id === userID && typeof pair.wave === 'number' && pair.wave <= (memoryCase.run_after_wave || 0));
	if (visible.length) return visible.sort((a, b) => b.wave - a.wave)[0];
	return candidates.find((pair) => pair.source === 'tool prerequisite' && pair.user_id === userID)
		|| candidates.find((pair) => pair.user_id === userID)
		|| candidates[0];
}

function detailHeader(kind, label, id) {
  return `<header class="detail-header">
    <div class="detail-kicker"><span class="badge ${kind}">${escapeHTML(kind)}</span><span class="badge">${escapeHTML(label || 'uncategorized')}</span></div>
    <h2>${escapeHTML(label || 'Dataset item')}</h2>
    <div class="detail-id">${escapeHTML(id)}</div>
  </header>`;
}

function humanSection(title, text, meta, className = '') {
  return `<section class="section"><div class="section-heading"><h3>${escapeHTML(title)}</h3><span>${escapeHTML(meta || '')}</span></div><p class="human-text ${className}">${escapeHTML(text || '—')}</p></section>`;
}

function qualitySection(signals) {
  if (!signals.length) return '';
  return `<section class="section"><div class="section-heading"><h3>Review signals</h3><span>heuristics, not verdicts</span></div><div class="quality-signals">${signals.map((signal) => `<div class="quality-signal">${escapeHTML(signal)}</div>`).join('')}</div></section>`;
}

function renderEvidenceRecords(records, terms, requiredIDs) {
  const required = new Set(requiredIDs);
  els.evidenceList.innerHTML = records.length ? records.map((pair) => {
    const meta = [`wave ${pair.wave}`, pair.user_id || 'miner', pair.session_id || 'no session', `${(pair.prompt || '').length} chars`];
    return `<article class="evidence-record ${required.has(pair.pair_id) ? 'required' : ''}">
      <div class="evidence-meta"><span>${escapeHTML(shortID(pair.pair_id, 12))}</span><span>${meta.map(escapeHTML).join(' · ')}</span></div>
      <div class="evidence-body"><p>${highlight(pair.prompt || '', terms)}</p><p class="response">${highlight(pair.response || '', terms)}</p></div>
    </article>`;
  }).join('') : '<div class="empty-state"><strong>No evidence records selected.</strong><span>Use Full timeline to inspect the complete seeded world.</span></div>';
}

function toolSignals(c) {
  const signals = [];
  if (c.fuzzy_trajectory) signals.push('Outcome-driven: expected tools matter, but their exact order does not.');
  if (c.allow_extra_tools) signals.push('The call count is an expected envelope, not a hard cap.');
  if ((c.prerequisite_pairs || []).length >= 3) signals.push(`Requires ${c.prerequisite_pairs.length} seed-bound context records before acting.`);
  if ((c.prompt || '').length > 600) signals.push('Long task prompt; inspect whether every clause reads like one plausible request.');
  return signals;
}

function rankToolEvidence(c, pairs, limit) {
	const stop = new Set(['about', 'after', 'already', 'before', 'check', 'complete', 'current', 'exact', 'figure', 'from', 'give', 'have', 'into', 'mention', 'rather', 'result', 'should', 'their', 'there', 'these', 'they', 'this', 'through', 'tool', 'use', 'which', 'with', 'your']);
	const queryTokens = new Set(String(c.prompt || '').toLowerCase().match(/[a-z0-9@.-]{4,}/g)?.filter((token) => !stop.has(token)) || []);
	const exactValues = (c.expected_tools || []).flatMap((tool) => Object.values(tool.required_args || {})).map(String).filter((value) => value.length >= 3);
	return pairs.map((pair) => {
		const body = normalizeSpace(`${pair.prompt || ''} ${pair.response || ''}`).toLowerCase();
		let score = 0;
		for (const value of exactValues) if (body.includes(value.toLowerCase())) score += 20;
		for (const token of queryTokens) if (body.includes(token)) score += 1;
		return { ...pair, wave: 'prerequisite', user_id: 'miner', source: 'tool prerequisite', reviewScore: score };
	}).filter((pair) => pair.reviewScore > 0).sort((a, b) => b.reviewScore - a.reviewScore || a.pair_id.localeCompare(b.pair_id)).slice(0, limit);
}

function memoryCaseSignals(c, evidence, annotation) {
  const signals = [];
  if (annotation) signals.push(`${annotation.required_pair_ids.length} exact planted records are required by the generation-time oracle.`);
  const literal = evidence.some((pair) => answerTerms(c).some((term) => normalizeSpace(`${pair.prompt} ${pair.response}`).toLowerCase().includes(term.toLowerCase())));
  if (answerTerms(c).length && literal) signals.push('At least one accepted answer form appears literally in the selected evidence.');
  if (answerTerms(c).length && !literal) signals.push('No accepted answer form appears literally; the case depends on composition, conversion, or negative evidence.');
  if ((c.question || '').length > 350) signals.push('Question is unusually long; check whether a real user would ask it in one turn.');
  if ((c.distractor_answers || []).length >= 3) signals.push(`${c.distractor_answers.length} same-attribute distractors compete with the oracle answer.`);
  return signals;
}

function memoryRecordSignals(pair) {
  const signals = [];
  const length = (pair.prompt || '').length;
  if (length >= 1800) signals.push(`Long-form memory (${length.toLocaleString()} characters); check narrative rhythm and whether facts are naturally buried.`);
  if (length < 80) signals.push('Very short memory; useful for natural length variation, but verify it carries enough conversational context.');
  for (const opener of paragraphOpeners(pair.prompt || '')) {
    const count = state.repeatedOpeners.get(opener) || 0;
    if (count >= 3) {
      signals.push(`Opening “${opener}…” repeats across ${count} paragraphs in this dataset.`);
      break;
    }
  }
  const responseCount = state.repeatedResponses.get(normalizeSpace(pair.response || '')) || 0;
  if (responseCount >= 4) signals.push(`This exact agent response repeats ${responseCount} times.`);
  if (/\b1\s+(days|weeks|months|years|hours|minutes)\b/i.test(pair.prompt || '')) signals.push('Possible singular/plural grammar error (“1” with a plural unit).');
  return signals;
}

function literalEvidence(c) {
  const terms = answerTerms(c);
  if (!terms.length) return [];
  return state.pairs.filter((pair) => {
    if ((c.user_id || 'miner') !== pair.user_id) return false;
    const body = normalizeSpace(`${pair.prompt || ''} ${pair.response || ''}`).toLowerCase();
    return terms.some((term) => body.includes(term.toLowerCase()));
  }).slice(0, 30);
}

function answerTerms(c) {
  return [...new Set([c.expected_answer, ...(c.accept_any || []), ...(c.answer_items || [])]
    .filter((value) => value && !value.startsWith('(No information'))
    .map(String))];
}

function formatArgs(args) {
  if (!args || Object.keys(args).length === 0) return 'No exact arguments';
  return Object.entries(args).map(([key, value]) => `${escapeHTML(key)}=${escapeHTML(value)}`).join(' · ');
}

function formatValues(values) {
  return values?.length ? values.map((value) => `<code>${escapeHTML(value)}</code>`).join(', ') : 'None';
}

function renderFlagForm(item) {
  const flag = flagFor(currentDatasetKey(), item.key);
  els.flagReason.value = flag?.reason || 'unbelievable';
  els.flagNote.value = flag?.note || '';
  els.flagState.textContent = flag ? 'Flagged' : 'Unflagged';
  els.flagState.classList.toggle('saved', Boolean(flag));
  els.removeFlag.disabled = !flag;
}

function saveCurrentFlag(event) {
  event.preventDefault();
  const item = state.items.find((candidate) => candidate.key === state.selectedKey);
  if (!item) return;
  const datasetKey = currentDatasetKey();
  const existing = flagFor(datasetKey, item.key);
  const now = new Date().toISOString();
  state.flags = state.flags.filter((flag) => !(flag.dataset_key === datasetKey && flag.item_key === item.key));
  state.flags.push({
    dataset_key: datasetKey,
    dataset_sha256: state.response.dataset_sha256,
    bench_version: state.artifact.bench_version,
    run_size: state.response.run_size,
    seed: state.artifact.seed,
    item_key: item.key,
    item_kind: item.kind,
    item_type: item.type,
    item_id: item.data.id || item.data.pair_id,
    reason: els.flagReason.value,
    note: els.flagNote.value.trim(),
    snapshot: item.kind === 'pair' ? item.data.prompt : item.prompt,
    created_at: existing?.created_at || now,
    updated_at: now,
  });
  persistFlags();
  renderList();
  showToast('Flag saved locally');
}

function removeCurrentFlag() {
  if (!state.selectedKey) return;
  const datasetKey = currentDatasetKey();
  state.flags = state.flags.filter((flag) => !(flag.dataset_key === datasetKey && flag.item_key === state.selectedKey));
  persistFlags();
  renderList();
  showToast('Flag removed');
}

function loadFlags() {
  try {
    const parsed = JSON.parse(localStorage.getItem(FLAG_STORAGE_KEY) || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function persistFlags() {
  localStorage.setItem(FLAG_STORAGE_KEY, JSON.stringify(state.flags));
  updateFlagCount();
}

function flagFor(datasetKey, itemKey) {
  return state.flags.find((flag) => flag.dataset_key === datasetKey && flag.item_key === itemKey);
}

function currentDatasetKey() {
  return state.response ? `${state.artifact.bench_version}:${state.response.run_size}:${state.artifact.seed}:${state.response.dataset_sha256}` : '';
}

function currentFlags() {
  const key = currentDatasetKey();
  return state.flags.filter((flag) => flag.dataset_key === key);
}

function updateFlagCount() {
  els.flagTotal.textContent = String(currentFlags().length);
}

async function copyDeepLink() {
  updateURL();
  try {
    await navigator.clipboard.writeText(location.href);
    showToast('Deep link copied');
  } catch {
    showToast('Copy failed; use the browser address bar');
  }
}

function downloadArtifact() {
  if (!state.artifact) return;
  downloadJSON(`dittobench-v${state.artifact.bench_version}-${state.response.run_size}-${state.artifact.seed}.json`, state.artifact);
}

function exportCurrentFlags() {
  const flags = currentFlags();
  downloadJSON(`dittobench-review-v${state.artifact.bench_version}-${state.artifact.seed}.json`, {
    schema_version: 1,
    exported_at: new Date().toISOString(),
    dataset: {
      bench_version: state.artifact.bench_version,
      run_size: state.response.run_size,
      seed: state.artifact.seed,
      dataset_sha256: state.response.dataset_sha256,
    },
    flags,
  });
  showToast(`Exported ${flags.length} flag${flags.length === 1 ? '' : 's'}`);
}

function downloadJSON(filename, value) {
  const blob = new Blob([`${JSON.stringify(value, null, 2)}\n`], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function updateURL() {
  if (!state.artifact) return;
  const query = new URLSearchParams({
    bench_version: String(state.artifact.bench_version),
    run_size: state.response.run_size,
    seed: String(state.artifact.seed),
    view: state.mode,
  });
  if (state.mode === 'memories') query.set('sort', state.memorySort);
  if (state.selectedKey) query.set('item', state.selectedKey);
  history.replaceState(null, '', `?${query}`);
}

function handleKeyboard(event) {
  const tag = event.target?.tagName?.toLowerCase();
  const editing = tag === 'input' || tag === 'textarea' || tag === 'select';
  if (event.key === '/' && !editing) {
    event.preventDefault();
    els.search.focus();
    return;
  }
  if (editing || !['ArrowDown', 'ArrowUp', 'j', 'k'].includes(event.key)) return;
  const direction = event.key === 'ArrowDown' || event.key === 'j' ? 1 : -1;
  const index = state.filteredItems.findIndex((item) => item.key === state.selectedKey);
  const next = state.filteredItems[Math.max(0, Math.min(state.filteredItems.length - 1, index + direction))];
  if (!next) return;
  event.preventDefault();
	const nextIndex = state.filteredItems.findIndex((item) => item.key === next.key);
	if (nextIndex >= state.renderLimit) {
		state.renderLimit = nextIndex + 1;
		renderList();
	}
  selectItem(next.key);
  els.itemList.querySelector(`[data-key="${cssEscape(next.key)}"]`)?.scrollIntoView({ block: 'nearest' });
}

function setLoading(loading) {
  els.workspace.setAttribute('aria-busy', String(loading));
  for (const control of els.form.elements) control.disabled = loading;
  if (loading) {
    els.summary.textContent = 'Generating deterministic dataset…';
    els.itemList.innerHTML = '<div class="empty-state"><strong>Building the world.</strong><span>This stays local.</span></div>';
  }
}

function showFatal(message) {
  els.summary.innerHTML = `<strong>Could not load dataset:</strong> ${escapeHTML(message)}`;
  els.itemList.innerHTML = '';
  els.inspector.innerHTML = '<div class="empty-state"><strong>Generation failed.</strong><span>Check the seed, version, and terminal output.</span></div>';
  els.workspace.setAttribute('aria-busy', 'false');
}

let toastTimer;
function showToast(message) {
  clearTimeout(toastTimer);
  els.toast.textContent = message;
  els.toast.classList.add('visible');
  toastTimer = setTimeout(() => els.toast.classList.remove('visible'), 2200);
}

async function fetchJSON(url) {
  const response = await fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

function paragraphOpeners(text) {
  return String(text || '').split(/\n\s*\n/).map((paragraph) => normalizeSpace(paragraph).toLowerCase().split(' ').slice(0, 7).join(' ')).filter((value) => value.split(' ').length >= 5);
}

function compareMemoryItems(a, b) {
  if (state.memorySort === 'category') {
    const categoryOrder = a.category.localeCompare(b.category);
    if (categoryOrder) return categoryOrder;
  }
  const timeOrder = timestampValue(a.data.timestamp) - timestampValue(b.data.timestamp);
  if (timeOrder) return timeOrder;
  return (a.data.dataset_index ?? 0) - (b.data.dataset_index ?? 0);
}

function memoryCategory(pair) {
  const session = String(pair.session_id || '').toLowerCase();
  if (session.startsWith('people-')) return 'People & relationships';
  if (session.startsWith('project-') || session === 'business-import') return 'Business & projects';
  if (session.startsWith('trip-')) return 'Travel';
  if (session.startsWith('story-')) return 'Long stories';
  if (session.startsWith('isolation-')) return 'Graph isolation';
  if (session.startsWith('preference')) return 'Preferences';
  if (session.startsWith('sess-')) return `Legacy · ${session.split('-').slice(0, 2).join('-')}`;
  const family = session.split('-')[0];
  return family ? family[0].toUpperCase() + family.slice(1) : 'Other';
}

function timestampValue(value) {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? Number.MAX_SAFE_INTEGER : parsed;
}

function formatTimestamp(value) {
  const parsed = timestampValue(value);
  if (parsed === Number.MAX_SAFE_INTEGER) return 'time unknown';
  return new Date(parsed).toISOString().replace('T', ' · ').slice(0, 18) + ' UTC';
}

function frequencyMap(values) {
  const out = new Map();
  for (const value of values.filter(Boolean)) out.set(value, (out.get(value) || 0) + 1);
  return out;
}

function highlight(text, terms) {
  const usable = terms.filter((term) => term && term.length >= 2).sort((a, b) => b.length - a.length);
  if (!usable.length) return escapeHTML(text);
  const regex = new RegExp(`(${usable.map(escapeRegExp).join('|')})`, 'gi');
  const accepted = new Set(usable.map((term) => term.toLowerCase()));
  return String(text).split(regex).map((part) => accepted.has(part.toLowerCase()) ? `<mark>${escapeHTML(part)}</mark>` : escapeHTML(part)).join('');
}

function escapeRegExp(value) { return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
function normalizeSpace(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
function shortID(value, size = 9) { const text = String(value || ''); return text.length > size ? `${text.slice(0, size)}…` : text; }
function cssEscape(value) { return window.CSS?.escape ? CSS.escape(value) : value.replace(/"/g, '\\"'); }
function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]);
}
