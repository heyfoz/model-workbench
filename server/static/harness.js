'use strict';
const byId = (id) => document.getElementById(id);
const provider = byId('provider'),
  model = byId('model'),
  prompt = byId('prompt');
const sessions = new Map();
let profiles = [];
let busy = false;

function currentProfile() {
  return profiles.find((item) => item.id === provider.value);
}
function session() {
  const key = JSON.stringify([provider.value, model.value]);
  if (!sessions.has(key)) sessions.set(key, []);
  return sessions.get(key);
}
function status(message = '', error = false) {
  byId('status').textContent = message;
  byId('status').classList.toggle('error', error);
}
function option(value, text) {
  return new Option(text, value);
}
function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}
function showControls() {
  const hasTurns = session().length > 0;
  provider.disabled = busy || !profiles.length;
  model.disabled = busy || !model.value;
  byId('refresh').disabled = busy;
  byId('delivery').disabled = busy;
  byId('send').disabled = busy || !model.value || !prompt.value.trim();
  byId('clear').disabled = busy || !hasTurns;
  byId('export').disabled = !hasTurns;
  prompt.readOnly = busy;
  byId('example').disabled = busy || !model.value;
}
function showTurn(questionText, answerText = '') {
  const question = element('article', '', 'turn user');
  question.append(element('h2', 'You'), element('p', questionText));
  const answer = element('article', '', 'turn');
  const text = document.createTextNode(answerText);
  const paragraph = element('p', '');
  paragraph.append(text);
  answer.append(element('h2', currentProfile().label), paragraph);
  byId('turns').append(question, answer);
  return { answer, text };
}
function render() {
  const runs = session();
  byId('turns').replaceChildren();
  byId('empty').hidden = Boolean(runs.length);
  for (const run of runs) {
    const { answer } = showTurn(run.question, run.content);
    const detail = [run.model, (run.elapsed_ms / 1000).toFixed(2) + ' s'];
    detail.push(run.stream ? 'Streamed' : 'Full response');
    if (Number.isFinite(run.output_tokens)) detail.push(run.output_tokens + ' output tokens');
    answer.append(element('div', detail.join(' · '), 'meta'));
  }
  byId('replies').textContent = runs.length;
  for (const [id, key] of [
    ['tokens-in', 'input_tokens'],
    ['tokens-out', 'output_tokens'],
  ]) {
    byId(id).textContent =
      runs.length && runs.every((run) => Number.isFinite(run[key]))
        ? runs.reduce((total, run) => total + run[key], 0)
        : '—';
  }
  showControls();
}
function selectProfile(preferredModel) {
  const profile = currentProfile();
  model.replaceChildren(...(profile?.models || []).map((name) => option(name, name)));
  if (profile?.models.includes(preferredModel)) model.value = preferredModel;
  if (!model.value) model.replaceChildren(option('', 'No chat models'));
  byId('location').textContent = profile?.location || '';
  status(profile?.issue || '', Boolean(profile?.issue));
  render();
}
async function refresh() {
  const previous = provider.value,
    previousModel = model.value;
  busy = true;
  showControls();
  status('Looking for models…');
  try {
    const response = await fetch('/api/providers');
    if (!response.ok) throw Error('Could not load providers. Check the server configuration.');
    profiles = await response.json();
    provider.replaceChildren(...profiles.map((p) => option(p.id, p.label)));
    provider.value =
      profiles.find((p) => p.id === previous)?.id ||
      profiles.find((p) => p.models.length)?.id ||
      profiles[0]?.id ||
      '';
    selectProfile(previousModel);
  } catch (error) {
    status(error.message, true);
  } finally {
    busy = false;
    showControls();
  }
}
provider.onchange = () => selectProfile();
model.onchange = () => {
  status();
  render();
};
byId('refresh').onclick = refresh;
byId('example').onclick = () => {
  prompt.value = 'Explain a database index in one sentence.';
  prompt.focus();
  showControls();
};
prompt.oninput = showControls;
prompt.onkeydown = (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter' && !byId('send').disabled) {
    event.preventDefault();
    byId('composer').requestSubmit();
  }
};
byId('clear').onclick = () => {
  session().length = 0;
  status();
  render();
  prompt.focus();
};
byId('export').onclick = () => {
  const data = { provider: provider.value, model: model.value, runs: session() };
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }),
  );
  const link = element('a', '');
  link.href = url;
  link.download = 'chat-' + provider.value + '.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
async function receiveStream(response, question) {
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  byId('empty').hidden = true;
  const { text } = showTurn(question);
  status('Streaming from ' + model.value + '…');
  let buffer = '';
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) throw Error('The stream ended early. You can send the message again.');
      buffer += chunk.value;
      let boundary;
      while ((boundary = buffer.indexOf('\n')) >= 0) {
        const event = JSON.parse(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 1);
        if (event.type === 'error') throw Error(event.error);
        if (event.type === 'done') {
          const { type, ...stats } = event;
          return { content: text.data, ...stats };
        }
        if (event.type !== 'delta' || typeof event.content !== 'string')
          throw Error('The server sent an invalid stream event.');
        text.appendData(event.content);
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
byId('composer').onsubmit = async (event) => {
  event.preventDefault();
  if (byId('send').disabled) return;
  const runs = session();
  const question = prompt.value.trim();
  const stream = new FormData(byId('composer')).get('delivery') === 'stream';
  const messages = runs.flatMap((run) => [
    { role: 'user', content: run.question },
    { role: 'assistant', content: run.content },
  ]);
  messages.push({ role: 'user', content: question });
  busy = true;
  showControls();
  status('Waiting for ' + model.value + '…');
  try {
    const response = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: provider.value, model: model.value, messages, stream }),
    });
    if (response.status === 413)
      throw Error('This conversation is too long. Export it and start a new chat.');
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw Error(error.error || 'The request failed.');
    }
    const result = stream ? await receiveStream(response, question) : await response.json();
    runs.push({ question, stream, ...result });
    prompt.value = '';
    status();
  } catch (error) {
    status(error.message, true);
  } finally {
    busy = false;
    render();
    prompt.focus();
  }
};
refresh();
