# Preserve a volume during storage investigation

Use this operator-only module to keep an existing encrypted gp3 volume managed
and protected while the application receives a different volume. Nonzero bytes
can occur on a new EBS disk; use the verified-new-volume procedure in the parent
README when provenance is established. Preserve disks whose provenance is
uncertain rather than bypassing the bootstrap guard.

1. Pause infrastructure workflows. Back up the original remote state privately.
2. Configure a separate S3 backend key such as
   `preserved-data/VOLUME-ID/terraform.tfstate`, outside `career-platform/`.
3. Supply the existing volume's exact region, availability zone, size, and Name
   tag through an ignored local variable file. Initialize with the lockfile.
4. Import the existing volume as `aws_ebs_volume.preserved`. Require a plan with
   no changes and verify the imported volume ID before removing the original
   volume's address from the application state. Never apply a create plan here
   as a substitute for importing the volume.
5. Generate a fresh application plan. Verify that the original volume is absent
   from its destroy actions and remains present in this module's remote state.
   The application plan may create a new volume and replace an empty host and
   its attachment. Review any host replacement before applying.

Keep the backend configuration and variable file available to the operator.
The preserved volume continues to incur EBS storage charges. Retain it until its
contents and origin are understood and the owner authorizes its disposition.
Removing this module or its state is not a substitute for that review.
