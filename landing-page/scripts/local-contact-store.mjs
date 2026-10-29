import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';

/** Minimal D1-compatible insert binding for the isolated local marketing preview. */
export function createLocalContactStore(filePath) {
  let pending = Promise.resolve();
  return {
    prepare(sql) {
      if (!sql.includes('INSERT INTO contact_inquiries')) throw new Error('Unsupported local contact query');
      return {
        bind(...values) {
          return {
            run() {
              const operation = pending.then(async () => {
                const [id, firstName, lastName, email, company, inquiryType, message,
                  createdAt, rateKey, _sameRateKey, cutoff] = values;
                const directory = path.dirname(filePath);
                await fs.mkdir(directory, {recursive: true, mode: 0o700});
                let rows = [];
                try {
                  rows = JSON.parse(await fs.readFile(filePath, 'utf8'));
                  if (!Array.isArray(rows)) throw new Error('Invalid local contact store');
                } catch (error) {
                  if (error.code !== 'ENOENT') throw error;
                }
                const recentCount = rows.filter(row => row.rate_key === rateKey && row.created_at > cutoff).length;
                if (recentCount >= 5) return {success: true, meta: {changes: 0}};
                rows.push({id, first_name: firstName, last_name: lastName, email, company,
                  inquiry_type: inquiryType, message, status: 'new', email_delivery: 'not_configured',
                  created_at: createdAt, rate_key: rateKey});
                const temporary = `${filePath}.${process.pid}.${randomUUID()}.tmp`;
                try {
                  await fs.writeFile(temporary, JSON.stringify(rows), {encoding: 'utf8', mode: 0o600, flag: 'wx'});
                  await fs.rename(temporary, filePath);
                  await fs.chmod(filePath, 0o600);
                } finally {
                  await fs.rm(temporary, {force: true});
                }
                return {success: true, meta: {changes: 1}};
              });
              pending = operation.catch(() => {});
              return operation;
            },
          };
        },
      };
    },
  };
}
