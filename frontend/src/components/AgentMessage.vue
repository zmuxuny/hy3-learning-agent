<script setup>
import MarkdownIt from 'markdown-it';
import { computed } from 'vue';

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
</script>

<template>
  <div class="agent-markdown" v-html="html"></div>
</template>
