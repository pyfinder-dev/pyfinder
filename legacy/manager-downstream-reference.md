# Historical manager and scheduler downstream calls

These exact commented snippets were removed from active modules during the
manager/scheduler integration cleanup. They are retained as historical context,
not executable examples or a supported fallback. Their imports, output paths,
and API assumptions belong to the former local workflow.

The underlying exporter, local trigger and ZIP collector already remain in
`legacy/shakemap.py`; they are not duplicated here. Current continuous execution
uses the separate service, while product collection and notifications still
require integration.

## Former failure-email construction

Former location: `pyfinder/findermanager.py`.

```python
        # Alert email remains inactive until terminal notification behavior is
        # owned by the external workflow boundary.
        # try:
        #     from services.alert import send_email_with_attachment
        #     subject = f"pyFinder Alert - event {event_id}"
        #     body = f"pyFinder attempted a shakemap calculation for {event_id},\n"
        #     body += f"but FinDer executable failed to produce a solution for the event.\n"
        #     body += f"Check the FinDer logs for more details.\n"
        #
        #     send_email_with_attachment(
        #         subject=subject,
        #         body=body,
        #         attachments=[attachment],
        #         event_id=event_id,
        #         finder_solution=None,
        #         metadata=self.metadata
        #     )
        #     self.logger.info(f"Failure notification sent.")
        #
        # except Exception as e:
        #     self.logger.error(f"Failed to send failure notification: {e}")
```

## Former failure notification call

Former location: `pyfinder/findermanager.py`.

```python
                # Failure notification remains inactive with downstream email.
                # self._send_failure_email(
                #     event_id=event_id,
                #     attachment=os.path.join(
                #         executable.get_working_directory(), "pyfinder.log")
                # )
```

## Former local ShakeMap export, execution, archive and success email

Former location: `pyfinder/findermanager.py`.

```python
            # Local ShakeMap execution and success email remain inactive until
            # the external service workflow is implemented.
            # Build a new eventid with the scheduled delay time and export the data for shakemap
            # from utils.shakemap import ShakeMapExporter
            # augmented_event_id = self._build_augmented_event_id(
            #     event_id=event_id, delay_minutes=self.metadata['current_delay'])
            # self.logger.info(f"Augmented event id for shakemap is {augmented_event_id}")
            #
            # Check if we are passing the amplitudes from FinDer output
            # use_finder_amplitudes = self.configuration.get("shakemap", {}).get("use-amplitude-from-finder-output", False)
            # self.logger.info(f"To ShakeMap :: Are you passing the amplitudes from FinDer output? {use_finder_amplitudes}")
            #
            # smap_exporter = ShakeMapExporter(
            #     solution=executable.get_finder_solution_object(),
            #     augmented_id=augmented_event_id,
            #     logger=self.logger)
            # shakemapexp = smap_exporter.export_all()
            # self.logger.info(f"ShakeMap files exported to: {shakemapexp['output_dir']}")
            #
            # Trigger ShakeMap using exported files
            # from utils.shakemap import ShakeMapTrigger
            # Create the products directory
            # products_dir = os.path.join(shakemapexp["output_dir"], "products")
            # os.makedirs(products_dir, exist_ok=True)
            # Copy the ShakeMap files to the products directory
            # trigger = ShakeMapTrigger(
            #     event_id=augmented_event_id,#event_id,
            #     event_xml=shakemapexp["event.xml"],
            #     stationlist_path=shakemapexp["stationlist.json"],
            #     rupture_path=shakemapexp["rupture.json"]
            # )
            # trigger.run()
            #
            # Archive the products via ShakeMap exporter under the temp_data directory
            # smap_exporter.archive_products(target_base_dir=self.finder_temp_data_dir)
            #
            # from services.alert import send_email_with_attachment
            # products_dir = os.path.join(shakemapexp["output_dir"], "products")
            # attachment = f"{products_dir}/intensity.jpg"
            # subject = f"pyFinder Alert - event {event_id}"
            # body = f"A new ShakeMap has been produced for event {event_id}.\n"
            # send_email_with_attachment(
            #     subject=subject,
            #     body=body,
            #     attachments=[attachment],
            #     event_id=event_id,
            #     finder_solution=executable.get_finder_solution_object(),
            #     metadata=self.metadata
            # )
```

## Former scheduler-local configuration download

Former location: `pyfinder/services/scheduler.py`.

```python
        # Local ShakeMap configuration is inactive until the external service
        # boundary is implemented.
        # try:
        #     from pyfinder.utils.config_fetcher import ensure_shakemap_config
        #
        #     self.logger.info("Ensuring ShakeMap configuration is available...")
        #     ensure_shakemap_config()
        #     self.logger.info("ShakeMap configuration cloned successfully.")
        # except Exception as e:
        #     self.logger.error(f"Failed to ensure ShakeMap configuration: {e}")
```

