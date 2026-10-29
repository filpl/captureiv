import { sqliteTable, text, integer, index } from 'drizzle-orm/sqlite-core';
export const inquiries=sqliteTable('contact_inquiries',{
 id:text('id').primaryKey(),firstName:text('first_name').notNull(),lastName:text('last_name').notNull(),
 email:text('email').notNull(),company:text('company').notNull(),inquiryType:text('inquiry_type').notNull(),
 message:text('message').notNull(),status:text('status').notNull().default('new'),
 emailDelivery:text('email_delivery').notNull().default('not_configured'),
 createdAt:integer('created_at').notNull(),rateKey:text('rate_key').notNull()
},t=>[index('inquiry_rate_created').on(t.rateKey,t.createdAt)]);
