<script setup>
import { computed } from 'vue';

const props = defineProps({
  content: { type: String, default: '' },
});

function escapeHtml(value) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function inline(value) {
  return value
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

function render(value) {
  const escaped = escapeHtml(value.trim());
  const blocks = escaped.split(/(```[\s\S]*?```)/g);
  return blocks.map((block) => {
    if (block.startsWith('```')) {
      const code = block.replace(/^```[^\n]*\n?/, '').replace(/```$/, '');
      return `<pre><code>${code}</code></pre>`;
    }
    const lines = block.split('\n');
    const output = [];
    let listType = '';
    const closeList = () => {
      if (listType) output.push(`</${listType}>`);
      listType = '';
    };
    for (const line of lines) {
      const unordered = line.match(/^\s*[-*]\s+(.+)/);
      const ordered = line.match(/^\s*\d+[.)]\s+(.+)/);
      if (unordered || ordered) {
        const nextType = unordered ? 'ul' : 'ol';
        if (listType !== nextType) {
          closeList();
          output.push(`<${nextType}>`);
          listType = nextType;
        }
        output.push(`<li>${inline((unordered || ordered)[1])}</li>`);
        continue;
      }
      closeList();
      if (!line.trim()) continue;
      const heading = line.match(/^#{1,3}\s+(.+)/);
      output.push(heading ? `<h3>${inline(heading[1])}</h3>` : `<p>${inline(line)}</p>`);
    }
    closeList();
    return output.join('');
  }).join('');
}

const html = computed(() => render(props.content));
</script>

<template>
  <div class="agent-markdown" v-html="html"></div>
</template>
