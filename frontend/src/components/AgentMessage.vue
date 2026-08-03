<script setup>
import MarkdownIt from 'markdown-it';
import { computed, nextTick, ref, watch } from 'vue';

const props = defineProps({
  content: { type: String, default: '' },
});

const markdown = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: true,
  typographer: false,
});

const defaultLinkOpen = markdown.renderer.rules.link_open
  || ((tokens, index, options, _env, self) => self.renderToken(tokens, index, options));
markdown.renderer.rules.link_open = (tokens, index, options, env, self) => {
  const token = tokens[index];
  token.attrSet('target', '_blank');
  token.attrSet('rel', 'noreferrer noopener');
  return defaultLinkOpen(tokens, index, options, env, self);
};

const html = computed(() => markdown.render(props.content || ''));
const root = ref(null);

watch(html, async () => {
  await nextTick();
  attachCopyButtons();
});

function attachCopyButtons() {
  if (!root.value) return;
  root.value.querySelectorAll('pre:not([data-copy-attached])').forEach((pre) => {
    pre.setAttribute('data-copy-attached', '1');
    const button = document.createElement('button');
    button.className = 'code-copy';
    button.textContent = '复制';
    button.addEventListener('click', async () => {
      const text = pre.querySelector('code')?.innerText || '';
      await navigator.clipboard?.writeText(text);
      button.textContent = '已复制';
      setTimeout(() => { button.textContent = '复制'; }, 1200);
    });
    pre.appendChild(button);
  });
}
</script>

<template>
  <div ref="root" class="agent-markdown" v-html="html"></div>
</template>
