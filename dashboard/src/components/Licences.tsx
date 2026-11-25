import { noticesTitle } from '@/lib/notices';

/**
 * The third-party notices as plain text. A server component: no script of its own, so the page
 * works under the strict Content-Security-Policy with nothing to allow.
 */
export function Licences({ text }: { text: string }) {
  return (
    <section aria-labelledby="licences-heading">
      <h2 id="licences-heading">Open-source licences</h2>
      <p>
        The IRA CARE dashboard is built with open-source software. These are the third-party packages
        inside it, their copyright lines and the full text of every licence they come under.
      </p>
      <p className="muted">{noticesTitle(text)}</p>
      <pre className="notices" tabIndex={0} aria-label="Third-party notices" data-testid="notices">{text}</pre>
    </section>
  );
}
