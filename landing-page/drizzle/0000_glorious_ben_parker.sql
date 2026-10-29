CREATE TABLE `contact_inquiries` (
	`id` text PRIMARY KEY NOT NULL,
	`first_name` text NOT NULL,
	`last_name` text NOT NULL,
	`email` text NOT NULL,
	`company` text NOT NULL,
	`inquiry_type` text NOT NULL,
	`message` text NOT NULL,
	`status` text DEFAULT 'new' NOT NULL,
	`email_delivery` text DEFAULT 'not_configured' NOT NULL,
	`created_at` integer NOT NULL,
	`rate_key` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `inquiry_rate_created` ON `contact_inquiries` (`rate_key`,`created_at`);