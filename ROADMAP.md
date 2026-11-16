# Roadmap

What's planned for Till, roughly in order. Each item is ticked off when
it's published here. Plans change, so this is a guide rather than a
promise. Ideas are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Groundwork

- [x] The repository basics: security notes, a contributing guide, issue
      templates, a changelog and this roadmap.
- [ ] Tests run automatically on GitHub for every change, on Python 3.11,
      3.12 and 3.13, and with the oldest versions of each dependency
      Till says it supports.

## Everyday gaps

- [ ] **Export for an accountant.** Invoices, payments, expenses and the
      journal as CSV files for a date range, which open correctly in
      Excel.
- [ ] **Search** the invoice and customer lists by number, name, email or
      phone.
- [ ] **Default payment terms.** The number of days until an invoice is
      due becomes a setting, instead of a fixed 14.
- [ ] **Invoice numbering settings.** Your own prefix for invoices and for
      quotes, and a starting number, so you can carry on the sequence
      from another tool.
- [ ] **Edit an expense.** Correcting an expense reverses the old entry
      and posts the new one, shown together as one corrected expense.
- [ ] **Import customers** from a CSV file, with a preview and duplicates
      flagged before anything is saved.
- [ ] **Calendar export that other apps read correctly.** All-day entries
      stay all-day, and timed entries carry your time zone.

## Bigger pieces

- [ ] **Discounts and credit notes.** A discount line on an invoice, and a
      credit note against an invoice that has been sent or paid. Credit on
      a paid invoice becomes a credit balance for the customer.
- [ ] **Tax collected for a period,** by rate, checked against the books.
- [ ] **Customer statements:** a printable page per customer, with invoices,
      credit notes, payments and the running balance.
- [ ] **Prices that include tax,** for VAT and GST, with "Tax invoice"
      wording.
- [ ] **Your logo on invoices.**
- [ ] **The same look on every computer,** with a bundled font or a
      standard system one.
- [ ] **Reaching Till from another device.** First, instructions for doing
      it safely over an SSH tunnel. A proper login for network use would
      be a separate, larger step.
- [ ] **Email an invoice** as a PDF, through your own mail server.

## Tidying up

- [ ] Clearer handling and logging of unexpected errors.
- [ ] Two rare edge cases in restoring a backup. No data is at risk.
- [ ] A clearer message for a draft that can't be posted because it's in
      another currency.
