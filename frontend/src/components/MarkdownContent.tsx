import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export default function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="markdown-content">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          table: ({ children }) => (
            <div className="markdown-table-wrapper">
              <table>{children}</table>
            </div>
          ),
          a: ({ children, href }) => <a href={href} target="_blank" rel="noreferrer">{children}</a>,
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
